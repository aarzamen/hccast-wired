"""Owned, private tegrastats capture for one supervised Jetson checkpoint."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
from typing import BinaryIO, Protocol


_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class TelemetryProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...

    def wait(self, timeout: float) -> int: ...


class TelemetryLauncher(Protocol):
    def launch(self, argv: Sequence[str], *, stdout: BinaryIO) -> TelemetryProcess: ...

    def stop(self, process: TelemetryProcess, *, grace: float) -> int: ...


class SubprocessTelemetryLauncher:
    """Launch and stop only the tegrastats process group created here."""

    def launch(
        self,
        argv: Sequence[str],
        *,
        stdout: BinaryIO,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            start_new_session=True,
            text=False,
        )

    def stop(self, process: TelemetryProcess, *, grace: float) -> int:
        code = process.poll()
        if code is not None:
            return code
        os.killpg(process.pid, signal.SIGTERM)
        try:
            return process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            return process.wait(timeout=grace)


@dataclass(frozen=True, slots=True)
class TelemetryResult:
    """Final process and durable-log status for one telemetry capture."""

    log_path: str
    returncode: int | None
    success: bool
    error: str | None

    def to_mapping(self) -> dict[str, object]:
        return {
            "log_path": self.log_path,
            "returncode": self.returncode,
            "success": self.success,
            "error": self.error,
        }


def _reject_symlink_ancestry(path: Path) -> None:
    current = path
    while True:
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise ValueError("telemetry root ancestry must not contain a symlink")
        if current.parent == current:
            return
        current = current.parent


class TelemetryRecorder:
    """Own one five-second tegrastats stream and an append-only private log."""

    def __init__(
        self,
        *,
        process: TelemetryProcess,
        launcher: TelemetryLauncher,
        log_path: Path,
        log: BinaryIO,
    ) -> None:
        self._process = process
        self._launcher = launcher
        self.log_path = log_path
        self._log = log
        self._result: TelemetryResult | None = None

    @classmethod
    def start(
        cls,
        root: Path,
        *,
        launcher: TelemetryLauncher,
        token: str,
        executable: str = "/usr/bin/tegrastats",
        interval_ms: int = 5000,
    ) -> TelemetryRecorder:
        root = Path(root)
        normalized = Path(os.path.normpath(root))
        if not root.is_absolute() or normalized != root or ".." in root.parts:
            raise ValueError("telemetry root must be an absolute normalized path")
        if _TOKEN.fullmatch(token) is None:
            raise ValueError("telemetry token is invalid")
        if not Path(executable).is_absolute():
            raise ValueError("telemetry executable must be absolute")
        if interval_ms <= 0:
            raise ValueError("telemetry interval must be positive")

        _reject_symlink_ancestry(root)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _reject_symlink_ancestry(root)
        if not stat.S_ISDIR(os.lstat(root).st_mode):
            raise ValueError("telemetry root must be a directory")
        os.chmod(root, 0o700)

        log_path = root / f"tegrastats-{token}.log"
        descriptor = os.open(
            log_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        log = os.fdopen(descriptor, "wb", closefd=True)
        try:
            process = launcher.launch(
                (executable, "--interval", str(interval_ms)),
                stdout=log,
            )
        except BaseException:
            log.close()
            log_path.unlink(missing_ok=True)
            raise
        return cls(
            process=process,
            launcher=launcher,
            log_path=log_path,
            log=log,
        )

    def poll_failure(self) -> str | None:
        code = self._process.poll()
        return None if code is None else f"telemetry-exited:{code}"

    def close(self) -> TelemetryResult:
        if self._result is not None:
            return self._result
        controlled_stop = self._process.poll() is None
        error: str | None = None
        code: int | None = None
        try:
            code = self._launcher.stop(self._process, grace=5.0)
        except (OSError, subprocess.SubprocessError) as caught:
            error = f"{caught.__class__.__name__}: {caught}"
        finally:
            try:
                self._log.flush()
                os.fsync(self._log.fileno())
            finally:
                self._log.close()

        success = error is None and (
            code == 0 or (controlled_stop and code == -signal.SIGTERM)
        )
        self._result = TelemetryResult(
            log_path=str(self.log_path),
            returncode=code,
            success=success,
            error=(
                error
                if error is not None
                else (None if success else f"tegrastats exited {code}")
            ),
        )
        return self._result
