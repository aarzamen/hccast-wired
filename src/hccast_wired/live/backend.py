"""Bounded, injectable subprocess implementation of the live-attempt protocol.

The backend owns only processes described by immutable command plans.  Physical
truth remains injectable and every production child/one-shot command receives its
own process group so cancellation and cleanup never target unrelated work.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import re
import signal
import subprocess
import threading
import time
from typing import Any, BinaryIO, Literal, Protocol, TypeAlias, cast

from hccast_wired.live.commands import LiveCommandPlan, ProcessSpec, ReconciliationPlan
from hccast_wired.live.evidence import RunEvidenceWriter
from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase
from hccast_wired.live.supervisor import (
    AttemptClassification,
    AttemptEvent,
    AttemptResult,
    CleanupError,
    CleanupResult,
    LiveAttempt,
)


PlanBuilder: TypeAlias = Callable[[LiveConfig], LiveCommandPlan | None]
_LifecycleState: TypeAlias = Literal[
    "CREATED", "RUNNING", "CLEANING", "FINISHED", "STOPPED_BEFORE_RUN"
]
_HANDSHAKE_MARKER = b"HCCAST handshake complete:"
_WAITING_MARKER = b"Enumerating directly as Android Open Accessory"
_SETR_MARKER = b"TX SETR"
_USERNAME = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
_STOCK_ACTIVE_ARGV = (
    "/usr/bin/systemctl",
    "is-active",
    "--quiet",
    "nv-l4t-usb-device-mode.service",
)
_STOCK_PROBE_ENV = {
    "HOME": "/root",
    "USER": "root",
    "LOGNAME": "root",
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}


@dataclass(frozen=True, slots=True)
class ReconciliationTargets:
    """The one UDC and filesystem locations a deployment has authorized."""

    expected_udc: str
    hccast_root: Path = Path("/sys/kernel/config/usb_gadget/hccast")
    functionfs_mountpoint: Path = Path("/dev/ffs-hccast")
    l4t_udc_path: Path = Path("/sys/kernel/config/usb_gadget/l4t/UDC")
    x_socket_path: Path = Path("/tmp/.X11-unix/X99")

    def __post_init__(self) -> None:
        if not self.expected_udc:
            raise ValueError("expected_udc must not be empty")


@dataclass(frozen=True, slots=True)
class BackendTiming:
    """All waits, injectable so tests never need production-duration sleeps."""

    x_readiness: float = 10.0
    handshake: float = 125.0
    one_shot: float = 15.0
    term_grace: float = 3.0
    kill_reap: float = 3.0
    poll_interval: float = 0.05

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.x_readiness,
                self.handshake,
                self.one_shot,
                self.term_grace,
                self.kill_reap,
                self.poll_interval,
            )
        ):
            raise ValueError("backend timing values must be positive")


class ReconciliationProbe(Protocol):
    def path_exists(self, path: Path) -> bool: ...
    def is_mountpoint(self, path: Path) -> bool: ...
    def read_text(self, path: Path) -> str: ...
    def owners_for_udc(self, expected_udc: str) -> frozenset[str]: ...
    def stock_service_active(self) -> bool: ...


class CommandRunner(Protocol):
    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        poll_interval: float,
        term_grace: float,
        kill_reap: float,
    ) -> int: ...


class ProcessLauncher(CommandRunner, Protocol):
    def launch(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        stdin: Any,
        stdout: Any,
        stderr: Any,
    ) -> subprocess.Popen[bytes]: ...

    def run_interruptible(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        interrupted: threading.Event,
        poll_interval: float,
        term_grace: float,
        kill_reap: float,
    ) -> int | None: ...

    def effective_uid(self) -> int: ...
    def current_user(self) -> str: ...
    def passwd(self, user: str) -> object: ...
    def signal_group(self, pid: int, sig: signal.Signals) -> None: ...
    def process_group_exists(self, pgid: int) -> bool: ...
    def wait(self, process: subprocess.Popen[bytes], timeout: float) -> int: ...


class SystemProbe:
    """Production filesystem and stock-service truth through an injected launcher."""

    def __init__(
        self,
        *,
        launcher: CommandRunner,
        configfs_gadget_root: Path = Path("/sys/kernel/config/usb_gadget"),
        stock_active_argv: tuple[str, ...] = _STOCK_ACTIVE_ARGV,
        stock_probe_env: dict[str, str] | None = None,
        timing: BackendTiming = BackendTiming(),
    ) -> None:
        self._launcher = launcher
        self._configfs_gadget_root = Path(configfs_gadget_root)
        self._stock_active_argv = stock_active_argv
        self._stock_probe_env = dict(stock_probe_env or _STOCK_PROBE_ENV)
        self._timing = timing

    def path_exists(self, path: Path) -> bool:
        return path.exists()

    def is_mountpoint(self, path: Path) -> bool:
        return os.path.ismount(path)

    def read_text(self, path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def owners_for_udc(self, expected_udc: str) -> frozenset[str]:
        owners: set[str] = set()
        gadgets = tuple(self._configfs_gadget_root.iterdir())
        for gadget in gadgets:
            if (gadget / "UDC").read_text(encoding="utf-8").strip() == expected_udc:
                owners.add(gadget.name)
        return frozenset(owners)

    def stock_service_active(self) -> bool:
        code = self._launcher.run(
            self._stock_active_argv,
            env=dict(self._stock_probe_env),
            timeout=self._timing.one_shot,
            poll_interval=self._timing.poll_interval,
            term_grace=self._timing.term_grace,
            kill_reap=self._timing.kill_reap,
        )
        if code == 0:
            return True
        if code == 3:
            return False
        raise RuntimeError(f"stock service status command returned {code}")


class SubprocessLauncher:
    """The only production subprocess boundary; every invocation has no shell."""

    def __init__(
        self,
        *,
        process_group_exists: Callable[[int], bool] | None = None,
    ) -> None:
        self._process_group_exists = process_group_exists or _system_process_group_exists

    def launch(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        stdin: Any,
        stdout: Any,
        stderr: Any,
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=env,
            shell=False,
            close_fds=True,
            start_new_session=True,
            text=False,
        )

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        poll_interval: float,
        term_grace: float,
        kill_reap: float,
    ) -> int:
        result = self._run_owned(
            argv,
            env=env,
            timeout=timeout,
            interrupted=None,
            poll_interval=poll_interval,
            term_grace=term_grace,
            kill_reap=kill_reap,
        )
        assert result is not None
        return result

    def run_interruptible(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        interrupted: threading.Event,
        poll_interval: float,
        term_grace: float,
        kill_reap: float,
    ) -> int | None:
        return self._run_owned(
            argv,
            env=env,
            timeout=timeout,
            interrupted=interrupted,
            poll_interval=poll_interval,
            term_grace=term_grace,
            kill_reap=kill_reap,
        )

    def _run_owned(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        interrupted: threading.Event | None,
        poll_interval: float,
        term_grace: float,
        kill_reap: float,
    ) -> int | None:
        if interrupted is not None and interrupted.is_set():
            return None
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            shell=False,
            close_fds=True,
            start_new_session=True,
            text=False,
        )
        pgid = process.pid
        deadline = time.monotonic() + timeout
        while True:
            code = process.poll()
            if code is not None:
                _terminate_process_group(
                    process,
                    term_grace,
                    kill_reap,
                    poll_interval=poll_interval,
                    pgid=pgid,
                    process_group_exists=self._process_group_exists,
                )
                return code
            if interrupted is not None and interrupted.is_set():
                _terminate_process_group(
                    process,
                    term_grace,
                    kill_reap,
                    poll_interval=poll_interval,
                    pgid=pgid,
                    process_group_exists=self._process_group_exists,
                )
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_process_group(
                    process,
                    term_grace,
                    kill_reap,
                    poll_interval=poll_interval,
                    pgid=pgid,
                    process_group_exists=self._process_group_exists,
                )
                raise subprocess.TimeoutExpired(argv, timeout)
            if interrupted is None:
                time.sleep(min(poll_interval, remaining))
            else:
                interrupted.wait(min(poll_interval, remaining))

    def effective_uid(self) -> int:
        return os.geteuid()

    def current_user(self) -> str:
        return pwd.getpwuid(os.geteuid()).pw_name

    def passwd(self, user: str) -> object:
        return pwd.getpwnam(user)

    def signal_group(self, pid: int, sig: signal.Signals) -> None:
        os.killpg(pid, sig)

    def process_group_exists(self, pgid: int) -> bool:
        return self._process_group_exists(pgid)

    def wait(self, process: subprocess.Popen[bytes], timeout: float) -> int:
        return process.wait(timeout=timeout)


def _system_process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_for_group_absence(
    pgid: int,
    *,
    exists: Callable[[int], bool],
    reap_direct: Callable[[], int | None],
    timeout: float,
    poll_interval: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        reap_direct()
        if not exists(pgid):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(poll_interval, remaining))


def _terminate_process_group(
    process: subprocess.Popen[bytes],
    term_grace: float,
    kill_reap: float,
    *,
    poll_interval: float = 0.05,
    pgid: int | None = None,
    process_group_exists: Callable[[int], bool] = _system_process_group_exists,
) -> None:
    owned_pgid = process.pid if pgid is None else pgid
    try:
        os.killpg(owned_pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    if _wait_for_group_absence(
        owned_pgid,
        exists=process_group_exists,
        reap_direct=process.poll,
        timeout=term_grace,
        poll_interval=poll_interval,
    ):
        process.wait(timeout=0)
        return
    try:
        os.killpg(owned_pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if not _wait_for_group_absence(
        owned_pgid,
        exists=process_group_exists,
        reap_direct=process.poll,
        timeout=kill_reap,
        poll_interval=poll_interval,
    ):
        raise RuntimeError(f"process group {owned_pgid} did not quiesce")
    process.wait(timeout=0)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class _Child:
    name: str
    process: subprocess.Popen[bytes]
    pgid: int


class _HandshakeParser:
    """Bounded incremental parser for the exact marker and one SETV JSON object."""

    def __init__(self, *, limit: int) -> None:
        self._limit = limit
        self._before = bytearray()
        self._pre_total = 0
        self._payload = bytearray()
        self._marker_seen = False
        self.outcome: tuple[str, str] | None = None
        self.error: str | None = None

    @property
    def buffered_bytes(self) -> int:
        return len(self._payload if self._marker_seen else self._before)

    def feed_stdout(self, chunk: bytes) -> None:
        if self.outcome is not None or self.error is not None:
            return
        if not self._marker_seen:
            combined = bytes(self._before) + chunk
            marker_at = combined.find(_HANDSHAKE_MARKER)
            if marker_at < 0:
                self._pre_total += len(chunk)
                keep = 0
                maximum = min(len(combined), len(_HANDSHAKE_MARKER) - 1)
                for length in range(maximum, 0, -1):
                    if combined[-length:] == _HANDSHAKE_MARKER[:length]:
                        keep = length
                        break
                self._before[:] = combined[-keep:] if keep else b""
                if self._pre_total - keep > self._limit:
                    self.error = "handshake-json-too-large"
                return
            marker_start = self._pre_total - len(self._before) + marker_at
            if marker_start > self._limit:
                self.error = "handshake-json-too-large"
                self._before.clear()
                return
            payload_at = marker_at + len(_HANDSHAKE_MARKER)
            payload = combined[payload_at:]
            self._before.clear()
            self._marker_seen = True
        else:
            payload = chunk
        room = self._limit - len(self._payload)
        self._payload.extend(payload[: room + 1])
        if len(payload) > room:
            self.error = "handshake-json-too-large"
            del self._payload[self._limit :]
            return
        self._parse_if_complete()

    def stdout_eof(self) -> None:
        if self.outcome is None and self.error is None:
            self.error = (
                "handshake-json-incomplete"
                if self._marker_seen
                else "gadget-stdout-eof"
            )

    def _parse_if_complete(self) -> None:
        try:
            text = self._payload.decode("utf-8")
        except UnicodeDecodeError:
            self.error = "handshake-json"
            return
        stripped = text.lstrip()
        if not stripped:
            return
        try:
            value, _end = json.JSONDecoder().raw_decode(stripped)
        except json.JSONDecodeError as caught:
            incomplete_messages = {
                "Unterminated string starting at",
                "Expecting value",
                "Expecting property name enclosed in double quotes",
                "Expecting ',' delimiter",
                "Expecting ':' delimiter",
            }
            at_end = caught.pos >= len(stripped)
            unterminated = caught.msg == "Unterminated string starting at"
            if not at_end and not unterminated:
                self.error = "handshake-json"
            elif caught.msg not in incomplete_messages:
                self.error = "handshake-json"
            return
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("product"), str)
            or not isinstance(value.get("version"), str)
        ):
            self.error = "handshake-json"
            return
        self.outcome = value["product"], value["version"]


def _pump_binary(
    reader: BinaryIO,
    writer: BinaryIO,
    producer_poll: Callable[[], int | None],
) -> str:
    """Copy bytes exactly and return the stable reason when transport ends."""

    try:
        while True:
            chunk = reader.read(65536)
            if not chunk:
                return "encoder-exited" if producer_poll() is not None else "encoder-eof"
            offset = 0
            while offset < len(chunk):
                written = writer.write(chunk[offset:])
                if written is None or written <= 0:
                    return "stream-pipe"
                offset += written
            writer.flush()
    except (BrokenPipeError, OSError, ValueError):
        return "stream-pipe"


class SubprocessAttemptFactory:
    """Creates unstarted attempts and performs fresh stopped-mode reconciliation."""

    def __init__(
        self,
        *,
        build_plan: PlanBuilder,
        reconciliation: ReconciliationPlan,
        targets: ReconciliationTargets,
        evidence_root: Path,
        launcher: ProcessLauncher | None = None,
        probes: ReconciliationProbe | None = None,
        timing: BackendTiming = BackendTiming(),
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._build_plan = build_plan
        self._reconciliation = reconciliation
        self._targets = targets
        self._evidence_root = Path(evidence_root)
        self._launcher = launcher or SubprocessLauncher()
        self._timing = timing
        self._probes = probes or SystemProbe(
            launcher=self._launcher,
            configfs_gadget_root=targets.hccast_root.parent,
            timing=timing,
        )
        self._monotonic = monotonic
        self._utc_now = utc_now

    def create(self, config: LiveConfig) -> LiveAttempt:
        plan = self._build_plan(config)
        if config.mode is DesiredMode.STOPPED or plan is None:
            raise ValueError("cannot create an active attempt for stopped mode")
        return SubprocessAttempt(
            config=config,
            plan=plan,
            reconciliation=self._reconciliation,
            targets=self._targets,
            evidence_root=self._evidence_root,
            launcher=self._launcher,
            probes=self._probes,
            timing=self._timing,
            started_monotonic=self._monotonic(),
            monotonic=self._monotonic,
            utc_now=self._utc_now,
        )

    def reconcile_stopped(self) -> CleanupResult:
        return _checked_reconciliation(
            reconciliation=self._reconciliation,
            targets=self._targets,
            launcher=self._launcher,
            probes=self._probes,
            timing=self._timing,
        )


class SubprocessAttempt:
    """One single-use attempt with binary gadget pipes and cached checked cleanup."""

    def __init__(
        self,
        *,
        config: LiveConfig,
        plan: LiveCommandPlan,
        reconciliation: ReconciliationPlan,
        targets: ReconciliationTargets,
        evidence_root: Path,
        launcher: ProcessLauncher,
        probes: ReconciliationProbe,
        timing: BackendTiming,
        started_monotonic: float,
        monotonic: Callable[[], float],
        utc_now: Callable[[], datetime],
    ) -> None:
        self._config, self._plan, self._reconciliation = config, plan, reconciliation
        self._targets, self._evidence_root = targets, evidence_root
        self._launcher, self._probes, self._timing = launcher, probes, timing
        self._started_monotonic, self._monotonic, self._utc_now = (
            started_monotonic,
            monotonic,
            utc_now,
        )
        self._children: list[_Child] = []
        self._gadget: _Child | None = None
        self._workers: list[threading.Thread] = []
        self._worker_errors: list[str] = []
        self._worker_lock = threading.Lock()
        self._pump_result: str | None = None
        self._pump_done = threading.Event()
        self._gadget_stdout_eof = threading.Event()
        self._gadget_stdout_done = threading.Event()
        self._gadget_stderr_done = threading.Event()
        self._waiting_observed = threading.Event()
        self._setr_observed = threading.Event()
        self._lifecycle = threading.Condition()
        self._lifecycle_state: _LifecycleState = "CREATED"
        self._cleanup_result: CleanupResult | None = None
        self._mutation_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._active_interrupt: threading.Event | None = None
        self._evidence_started = False
        self._evidence_finalize_attempted = False
        self._writer: RunEvidenceWriter | None = None
        self._product: str | None = None
        self._version: str | None = None
        self._streaming_at: float | None = None

    @property
    def started_monotonic(self) -> float:
        return self._started_monotonic

    def _environment(self, spec: ProcessSpec) -> dict[str, str]:
        env = dict(spec.env)
        if spec.name == "gadget-stream":
            env["PYTHONUNBUFFERED"] = "1"
        if spec.run_as_user is None:
            return env
        if _USERNAME.fullmatch(spec.run_as_user) is None:
            raise ValueError("run_as_user is not a conservative POSIX account name")
        if self._launcher.effective_uid() == 0:
            record = self._launcher.passwd(spec.run_as_user)
            uid = getattr(record, "pw_uid")
            home = getattr(record, "pw_dir")
            prefix = (
                "/usr/sbin/runuser",
                "--user",
                spec.run_as_user,
                "--",
                "/usr/bin/env",
                "--ignore-environment",
                f"HOME={home}",
                f"USER={spec.run_as_user}",
                f"LOGNAME={spec.run_as_user}",
                "PATH=/usr/bin:/bin",
                f"DISPLAY=:{self._config.display_number}",
                f"XDG_RUNTIME_DIR=/run/user/{uid}",
            )
            replacement = {
                "HOME": str(home),
                "USER": spec.run_as_user,
                "LOGNAME": spec.run_as_user,
                "PATH": "/usr/bin:/bin",
                "DISPLAY": f":{self._config.display_number}",
                "XDG_RUNTIME_DIR": f"/run/user/{uid}",
            }
            return {"__backend_argv_prefix__": "\0".join(prefix)} | replacement
        if spec.run_as_user != self._launcher.current_user():
            raise PermissionError("non-root launch cannot impersonate another user")
        return env

    def _argv_and_env(self, spec: ProcessSpec) -> tuple[tuple[str, ...], dict[str, str]]:
        env = self._environment(spec)
        prefix = env.pop("__backend_argv_prefix__", None)
        return ((tuple(prefix.split("\0")) + spec.argv) if prefix else spec.argv, env)

    def _is_interrupted(self, interrupted: threading.Event) -> bool:
        return interrupted.is_set() or self._stop_requested.is_set()

    def _start(
        self,
        spec: ProcessSpec,
        interrupted: threading.Event,
        *,
        stdin: object = subprocess.DEVNULL,
        stdout: object = subprocess.PIPE,
        stderr: object = subprocess.PIPE,
    ) -> _Child | None:
        with self._mutation_lock:
            if self._is_interrupted(interrupted):
                return None
            argv, env = self._argv_and_env(spec)
            process = self._launcher.launch(
                argv,
                env=env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
            )
            child = _Child(spec.name, process, process.pid)
            self._children.append(child)
            return child

    def _run_active_command(
        self, spec: ProcessSpec, interrupted: threading.Event
    ) -> bool | None:
        if self._is_interrupted(interrupted):
            return None
        argv, env = self._argv_and_env(spec)
        try:
            code = self._launcher.run_interruptible(
                argv,
                env=env,
                timeout=self._timing.one_shot,
                interrupted=interrupted,
                poll_interval=self._timing.poll_interval,
                term_grace=self._timing.term_grace,
                kill_reap=self._timing.kill_reap,
            )
        except (OSError, subprocess.SubprocessError, PermissionError, ValueError):
            return False
        if code is None or self._is_interrupted(interrupted):
            return None
        return code == 0

    def _wait_until(
        self, predicate: Callable[[], bool], timeout: float, interrupted: threading.Event
    ) -> bool | None:
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            if self._is_interrupted(interrupted):
                return None
            if predicate():
                return True
            interrupted.wait(
                min(self._timing.poll_interval, max(0.0, deadline - self._monotonic()))
            )
        if self._is_interrupted(interrupted):
            return None
        return predicate()

    def run(
        self, publish: Callable[[AttemptEvent], None], interrupted: threading.Event
    ) -> AttemptResult | None:
        with self._lifecycle:
            if self._lifecycle_state == "STOPPED_BEFORE_RUN":
                return None
            if self._lifecycle_state != "CREATED":
                raise RuntimeError("an attempt may run only once")
            self._lifecycle_state = "RUNNING"
            self._active_interrupt = interrupted

        writer_start_error: Exception | None = None
        try:
            with self._mutation_lock:
                if self._stop_requested.is_set():
                    cleanup = self._settle_without_side_effects()
                    self._finish_lifecycle(cleanup)
                    return None
                try:
                    self._writer = RunEvidenceWriter.start(
                        self._evidence_root,
                        self._config,
                        utc_now=self._utc_now,
                        attempt_token=f"attempt-{id(self):x}",
                    )
                except Exception as caught:
                    writer_start_error = caught
                else:
                    self._evidence_started = True
        except BaseException:
            cleanup = self._settle_without_side_effects()
            self._finish_lifecycle(cleanup)
            raise

        if writer_start_error is not None:
            cleanup = self._settle_without_side_effects()
            self._finish_lifecycle(cleanup)
            return AttemptResult(
                AttemptClassification.FAILURE,
                f"startup-failed: {_message(writer_start_error)}",
                None,
                None,
                0.0,
                cleanup,
            )

        error: str | None = None
        was_interrupted = False
        base_interrupt: KeyboardInterrupt | SystemExit | None = None
        try:
            error = self._active_lifecycle(publish, interrupted)
            was_interrupted = error is None and self._is_interrupted(interrupted)
            if error is None and not was_interrupted:
                error = "stream-pipe"
        except (KeyboardInterrupt, SystemExit) as caught:
            base_interrupt = caught
        except (
            OSError,
            subprocess.SubprocessError,
            PermissionError,
            ValueError,
            KeyError,
            AttributeError,
            TypeError,
        ) as caught:
            error = f"startup-failed: {_message(caught)}"
        except Exception as caught:
            error = f"internal-failure: {_message(caught)}"

        cleanup = self._run_owned_cleanup()
        duration = self._streaming_duration()
        if base_interrupt is not None:
            evidence_result = AttemptResult(
                AttemptClassification.FAILURE,
                f"internal-interrupt: {_message(base_interrupt)}",
                self._product,
                self._version,
                duration,
                cleanup,
            )
            try:
                self._finalize_once(evidence_result, "internal-interrupt")
            except BaseException:
                pass
            finally:
                self._finish_lifecycle(cleanup)
            raise base_interrupt

        if was_interrupted:
            result = AttemptResult(
                AttemptClassification.COMPLETED,
                None,
                self._product,
                self._version,
                duration,
                cleanup,
            )
            terminal_reason = "interrupted"
        else:
            assert error is not None
            result = AttemptResult(
                AttemptClassification.FAILURE,
                error,
                self._product,
                self._version,
                duration,
                cleanup,
            )
            terminal_reason = _reason_prefix(error)

        try:
            self._finalize_once(result, terminal_reason)
        except Exception as caught:
            result = AttemptResult(
                AttemptClassification.FAILURE,
                f"evidence-failed: {_message(caught)}",
                self._product,
                self._version,
                duration,
                cleanup,
            )
            was_interrupted = False
        except (KeyboardInterrupt, SystemExit):
            self._finish_lifecycle(cleanup)
            raise
        self._finish_lifecycle(cleanup)
        return None if was_interrupted else result

    def _active_lifecycle(
        self, publish: Callable[[AttemptEvent], None], interrupted: threading.Event
    ) -> str | None:
        if self._is_interrupted(interrupted):
            return None
        if self._probes.path_exists(self._targets.x_socket_path):
            return "x-socket-preexisting"
        if self._is_interrupted(interrupted):
            return None

        xvfb = self._start(self._plan.xvfb, interrupted)
        if xvfb is None:
            return None
        self._start_source_drainers(xvfb)
        ready = self._wait_until(
            lambda: (
                xvfb.process.poll() is not None
                or self._probes.path_exists(self._targets.x_socket_path)
            ),
            self._timing.x_readiness,
            interrupted,
        )
        if ready is None:
            return None
        if xvfb.process.poll() is not None:
            return "source-exited"
        if not ready:
            return "x-readiness-timeout"
        if self._is_interrupted(interrupted):
            return None

        for spec in (
            self._plan.openbox,
            self._plan.x11vnc,
            self._plan.websockify,
            self._plan.chromium,
        ):
            if spec is None:
                continue
            if self._source_exited():
                return "source-exited"
            child = self._start(spec, interrupted)
            if child is None:
                return None
            self._start_source_drainers(child)
            if self._is_interrupted(interrupted):
                return None

        if self._source_exited():
            return "source-exited"
        stock_stopped = self._run_active_command(self._plan.stock_service_stop, interrupted)
        if stock_stopped is None:
            return None
        if not stock_stopped:
            return "stock-stop-failed"
        if self._is_interrupted(interrupted):
            return None
        if self._source_exited():
            return "source-exited"
        if self._probes.owners_for_udc(self._targets.expected_udc):
            return "udc-not-free"
        if self._is_interrupted(interrupted):
            return None

        gadget = self._start(self._plan.gadget, interrupted, stdin=subprocess.PIPE)
        if gadget is None:
            return None
        self._gadget = gadget
        parser = _HandshakeParser(limit=65536)
        parser_condition = threading.Condition()
        self._start_gadget_drainers(gadget, parser, parser_condition)
        handshake = self._wait_for_handshake(
            gadget, parser, parser_condition, interrupted, publish
        )
        if handshake is None or isinstance(handshake, str):
            return handshake
        self._product, self._version = handshake
        if self._is_interrupted(interrupted):
            return None
        if self._source_exited():
            return "source-exited"

        encoder = self._start(self._plan.encoder, interrupted)
        if encoder is None:
            return None
        self._start_encoder_stderr(encoder)
        self._start_pump(encoder, gadget)
        if self._is_interrupted(interrupted):
            return None
        self._publish(
            publish,
            AttemptEvent(RuntimePhase.STREAMING, self._product, self._version),
        )
        self._streaming_at = self._monotonic()

        while not self._is_interrupted(interrupted):
            if self._source_exited():
                return "source-exited"
            if gadget.process.poll() is not None:
                return "gadget-exited"
            if encoder.process.poll() is not None:
                return "encoder-exited"
            if self._gadget_stdout_eof.is_set():
                return "gadget-stdout-eof"
            if self._pump_done.is_set():
                if encoder.process.poll() is not None:
                    return "encoder-exited"
                return self._pump_result or "stream-pipe"
            with self._worker_lock:
                if self._worker_errors:
                    return "stream-pipe"
            interrupted.wait(self._timing.poll_interval)
        return None

    def _streaming_duration(self) -> float:
        if self._streaming_at is None:
            return 0.0
        return max(0.0, self._monotonic() - self._streaming_at)

    def _source_exited(self) -> bool:
        return any(
            child.name not in {"gadget-stream", "encoder"}
            and child.process.poll() is not None
            for child in self._children
        )

    def _publish(self, publish: Callable[[AttemptEvent], None], event: AttemptEvent) -> None:
        publish(event)
        if self._writer is not None:
            self._writer.record_transition(event)

    def _worker(self, name: str, target: Callable[[], None]) -> None:
        def guarded() -> None:
            try:
                target()
            except (OSError, ValueError) as caught:
                with self._worker_lock:
                    self._worker_errors.append(f"{name}: {_message(caught)}")

        worker = threading.Thread(
            target=guarded,
            name=f"hccast-live-{name}",
            daemon=False,
        )
        self._workers.append(worker)
        worker.start()

    def _drain_log(
        self,
        child: _Child,
        stream: str,
        reader: BinaryIO,
        log_name: Literal["source", "encoder"],
    ) -> None:
        while True:
            chunk = os.read(reader.fileno(), 4096)
            if not chunk:
                return
            if self._writer is not None:
                decoded = chunk.decode("utf-8", errors="replace")
                self._writer.append_log(log_name, f"[{child.name}][{stream}] {decoded}")

    def _start_source_drainers(self, child: _Child) -> None:
        assert child.process.stdout is not None and child.process.stderr is not None
        self._worker(
            f"{child.name}-stdout",
            lambda: self._drain_log(
                child, "stdout", cast(BinaryIO, child.process.stdout), "source"
            ),
        )
        self._worker(
            f"{child.name}-stderr",
            lambda: self._drain_log(
                child, "stderr", cast(BinaryIO, child.process.stderr), "source"
            ),
        )

    def _start_encoder_stderr(self, child: _Child) -> None:
        assert child.process.stderr is not None
        self._worker(
            "encoder-stderr",
            lambda: self._drain_log(
                child, "stderr", cast(BinaryIO, child.process.stderr), "encoder"
            ),
        )

    def _start_gadget_drainers(
        self,
        gadget: _Child,
        parser: _HandshakeParser,
        condition: threading.Condition,
    ) -> None:
        assert gadget.process.stdout is not None and gadget.process.stderr is not None
        stdout_tail = bytearray()
        stderr_tail = bytearray()

        def drain_stdout() -> None:
            reader = cast(BinaryIO, gadget.process.stdout)
            try:
                while True:
                    chunk = os.read(reader.fileno(), 4096)
                    if not chunk:
                        self._gadget_stdout_eof.set()
                        with condition:
                            parser.stdout_eof()
                            condition.notify_all()
                        return
                    if self._writer is not None:
                        self._writer.append_log(
                            "gadget", chunk.decode("utf-8", errors="replace")
                        )
                    stdout_tail.extend(chunk)
                    if _WAITING_MARKER in stdout_tail:
                        self._waiting_observed.set()
                    if len(stdout_tail) > len(_WAITING_MARKER) * 2:
                        del stdout_tail[: -len(_WAITING_MARKER) * 2]
                    with condition:
                        parser.feed_stdout(chunk)
                        condition.notify_all()
            except (OSError, ValueError) as caught:
                with self._worker_lock:
                    self._worker_errors.append(
                        f"gadget-stdout: {_message(caught)}"
                    )
            finally:
                self._gadget_stdout_done.set()
                with condition:
                    condition.notify_all()

        def drain_stderr() -> None:
            reader = cast(BinaryIO, gadget.process.stderr)
            try:
                while True:
                    chunk = os.read(reader.fileno(), 4096)
                    if not chunk:
                        return
                    if self._writer is not None:
                        self._writer.append_log(
                            "gadget", chunk.decode("utf-8", errors="replace")
                        )
                    stderr_tail.extend(chunk)
                    if _SETR_MARKER in stderr_tail:
                        self._setr_observed.set()
                    if len(stderr_tail) > len(_SETR_MARKER) * 2:
                        del stderr_tail[: -len(_SETR_MARKER) * 2]
                    with condition:
                        condition.notify_all()
            except (OSError, ValueError) as caught:
                with self._worker_lock:
                    self._worker_errors.append(
                        f"gadget-stderr: {_message(caught)}"
                    )
            finally:
                self._gadget_stderr_done.set()
                with condition:
                    condition.notify_all()

        self._worker("gadget-stdout", drain_stdout)
        self._worker("gadget-stderr", drain_stderr)

    def _wait_for_handshake(
        self,
        gadget: _Child,
        parser: _HandshakeParser,
        condition: threading.Condition,
        interrupted: threading.Event,
        publish: Callable[[AttemptEvent], None] | None = None,
    ) -> tuple[str, str] | str | None:
        deadline = self._monotonic() + self._timing.handshake
        waiting_published = False
        handshaking_published = False

        def publish_observed() -> None:
            nonlocal waiting_published, handshaking_published
            if publish is None:
                return
            if self._waiting_observed.is_set() and not waiting_published:
                self._publish(publish, AttemptEvent(RuntimePhase.WAITING_FOR_SCREEN))
                waiting_published = True
            if (
                waiting_published
                and self._setr_observed.is_set()
                and not handshaking_published
            ):
                self._publish(publish, AttemptEvent(RuntimePhase.HANDSHAKING))
                handshaking_published = True

        while self._monotonic() < deadline:
            if self._is_interrupted(interrupted):
                return None
            publish_observed()
            with condition:
                if parser.error is not None:
                    return parser.error
                gadget_alive = gadget.process.poll() is None
                if parser.outcome is not None:
                    self._product, self._version = parser.outcome
                    if (
                        gadget_alive
                        and self._waiting_observed.is_set()
                        and self._setr_observed.is_set()
                    ):
                        return parser.outcome
                if not gadget_alive:
                    break
                condition.wait(
                    min(
                        self._timing.poll_interval,
                        max(0.0, deadline - self._monotonic()),
                    )
                )
            with self._worker_lock:
                if self._worker_errors:
                    return "stream-pipe"
            if self._source_exited():
                return "source-exited"
        if gadget.process.poll() is not None:
            drain_deadline = min(
                deadline,
                self._monotonic() + self._timing.term_grace,
            )
            while not (
                self._gadget_stdout_done.is_set()
                and self._gadget_stderr_done.is_set()
            ):
                if self._is_interrupted(interrupted):
                    return None
                remaining = drain_deadline - self._monotonic()
                if remaining <= 0:
                    break
                with condition:
                    condition.wait(min(self._timing.poll_interval, remaining))
            publish_observed()
            with condition:
                if parser.error is not None:
                    return parser.error
                if parser.outcome is not None:
                    self._product, self._version = parser.outcome
            with self._worker_lock:
                if self._worker_errors:
                    return "stream-pipe"
            return "gadget-exited"
        publish_observed()
        with condition:
            if parser.error is not None:
                return parser.error
            if (
                parser.outcome is not None
                and self._waiting_observed.is_set()
                and self._setr_observed.is_set()
            ):
                return parser.outcome
        return "handshake-timeout"

    def _start_pump(self, encoder: _Child, gadget: _Child) -> None:
        assert encoder.process.stdout is not None and gadget.process.stdin is not None

        def pump() -> None:
            try:
                self._pump_result = _pump_binary(
                    cast(BinaryIO, encoder.process.stdout),
                    cast(BinaryIO, gadget.process.stdin),
                    encoder.process.poll,
                )
            finally:
                self._pump_done.set()

        self._worker("encoder-pump", pump)

    def _finalize_once(self, result: AttemptResult, terminal_reason: str) -> None:
        if self._evidence_finalize_attempted:
            raise RuntimeError("evidence finalization was already attempted")
        self._evidence_finalize_attempted = True
        assert self._writer is not None
        self._writer.finalize(result, terminal_reason=terminal_reason)

    def _settle_without_side_effects(self) -> CleanupResult:
        empty = CleanupResult((), (), (), True)
        with self._lifecycle:
            if self._cleanup_result is None:
                self._cleanup_result = empty
            return self._cleanup_result

    def _run_owned_cleanup(self) -> CleanupResult:
        with self._lifecycle:
            if self._cleanup_result is not None:
                return self._cleanup_result
            if self._lifecycle_state != "RUNNING":
                raise RuntimeError(
                    f"run-owned cleanup entered from {self._lifecycle_state}"
                )
            self._lifecycle_state = "CLEANING"
        try:
            result = self._cleanup()
        except BaseException as caught:
            result = CleanupResult(
                ("cleanup-internal",),
                (CleanupError("cleanup-internal", _message(caught)),),
                (),
                False,
            )
        with self._lifecycle:
            if self._cleanup_result is None:
                self._cleanup_result = result
            return self._cleanup_result

    def _finish_lifecycle(self, cleanup: CleanupResult) -> None:
        with self._lifecycle:
            if self._cleanup_result is None:
                self._cleanup_result = cleanup
            self._lifecycle_state = "FINISHED"
            self._lifecycle.notify_all()

    def stop(self) -> CleanupResult:
        with self._lifecycle:
            if self._lifecycle_state == "CREATED":
                self._stop_requested.set()
                result = CleanupResult((), (), (), True)
                self._cleanup_result = result
                self._lifecycle_state = "STOPPED_BEFORE_RUN"
                self._lifecycle.notify_all()
                return result
            if self._lifecycle_state == "STOPPED_BEFORE_RUN":
                assert self._cleanup_result is not None
                return self._cleanup_result
            self._stop_requested.set()
            if self._active_interrupt is not None:
                self._active_interrupt.set()
            self._lifecycle.wait_for(
                lambda: self._lifecycle_state in {"FINISHED", "STOPPED_BEFORE_RUN"}
            )
            assert self._cleanup_result is not None
            return self._cleanup_result

    def _cleanup(self) -> CleanupResult:
        actions: list[str] = []
        errors: list[CleanupError] = []
        named = {child.name: child for child in tuple(self._children)}
        order = [
            "encoder",
            "gadget-stream",
            "chromium",
            "websockify",
            "x11vnc",
            "openbox",
            "xvfb",
        ]
        for name in order:
            child = named.get(name)
            if child is None:
                continue
            if name == "gadget-stream" and child.process.stdin is not None:
                actions.append("gadget-stdin-close")
                try:
                    child.process.stdin.close()
                except (OSError, ValueError) as caught:
                    errors.append(CleanupError("gadget-stdin-close", _message(caught)))
                else:
                    try:
                        self._launcher.wait(child.process, self._timing.term_grace)
                    except subprocess.TimeoutExpired:
                        pass
                    except OSError as caught:
                        errors.append(CleanupError("gadget-stdin-close", _message(caught)))
            actions.append(f"stop-{name}")
            _stop_child(child, self._launcher, self._timing, errors)

        for worker in tuple(self._workers):
            worker.join(self._timing.kill_reap)
            if worker.is_alive():
                errors.append(CleanupError("worker-join", worker.name))

        reconciliation = _checked_reconciliation(
            reconciliation=self._reconciliation,
            targets=self._targets,
            launcher=self._launcher,
            probes=self._probes,
            timing=self._timing,
        )
        return CleanupResult(
            tuple(actions) + reconciliation.attempted_actions,
            tuple(errors) + reconciliation.errors,
            reconciliation.verified_postconditions,
            not errors and reconciliation.success,
        )


def _message(error: BaseException) -> str:
    return str(error).strip() or error.__class__.__name__


def _reason_prefix(error: str) -> str:
    return error.split(":", 1)[0]


def _stop_child(
    child: _Child,
    launcher: ProcessLauncher,
    timing: BackendTiming,
    errors: list[CleanupError],
) -> None:
    direct_exited = child.process.poll() is not None
    if direct_exited:
        try:
            launcher.wait(child.process, 0)
        except (OSError, subprocess.TimeoutExpired) as caught:
            errors.append(CleanupError(f"reap-{child.name}", _message(caught)))
    try:
        launcher.signal_group(child.pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except OSError as caught:
        errors.append(CleanupError(f"term-{child.name}", _message(caught)))
    term_deadline = time.monotonic() + timing.term_grace
    group_exists = True
    while True:
        child.process.poll()
        try:
            group_exists = launcher.process_group_exists(child.pgid)
        except OSError as caught:
            errors.append(CleanupError(f"group-probe-{child.name}", _message(caught)))
            return
        if not group_exists:
            break
        remaining = term_deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(timing.poll_interval, remaining))
    if not group_exists:
        if not direct_exited:
            try:
                launcher.wait(child.process, 0)
            except (OSError, subprocess.TimeoutExpired) as caught:
                errors.append(CleanupError(f"reap-{child.name}", _message(caught)))
        return
    try:
        launcher.signal_group(child.pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as caught:
        errors.append(CleanupError(f"kill-{child.name}", _message(caught)))
    kill_deadline = time.monotonic() + timing.kill_reap
    while True:
        child.process.poll()
        try:
            group_exists = launcher.process_group_exists(child.pgid)
        except OSError as caught:
            errors.append(CleanupError(f"group-probe-{child.name}", _message(caught)))
            return
        if not group_exists:
            break
        remaining = kill_deadline - time.monotonic()
        if remaining <= 0:
            errors.append(
                CleanupError(
                    f"group-reap-{child.name}",
                    f"process group {child.pgid} still present",
                )
            )
            break
        time.sleep(min(timing.poll_interval, remaining))
    try:
        launcher.wait(child.process, 0)
    except BaseException as caught:
        errors.append(CleanupError(f"reap-{child.name}", _message(caught)))


def _checked_reconciliation(
    *,
    reconciliation: ReconciliationPlan,
    targets: ReconciliationTargets,
    launcher: ProcessLauncher,
    probes: ReconciliationProbe,
    timing: BackendTiming,
) -> CleanupResult:
    actions: list[str] = []
    errors: list[CleanupError] = []
    verified: list[str] = []

    def command(spec: ProcessSpec) -> bool:
        actions.append(spec.name)
        try:
            code = launcher.run(
                spec.argv,
                env=dict(spec.env),
                timeout=timing.one_shot,
                poll_interval=timing.poll_interval,
                term_grace=timing.term_grace,
                kill_reap=timing.kill_reap,
            )
        except BaseException as caught:
            errors.append(CleanupError(spec.name, _message(caught)))
            return False
        if code != 0:
            errors.append(CleanupError(spec.name, f"exit {code}"))
            return False
        return True

    def probe(action: str, operation: Callable[[], Any]) -> tuple[bool, Any]:
        try:
            return True, operation()
        except BaseException as caught:
            errors.append(CleanupError(action, _message(caught)))
            return False, None

    command(reconciliation.gadget_cleanup)
    stock_ok, stock_active = probe("stock-service-active", probes.stock_service_active)
    owners_ok, owners = probe(
        "udc-owners", lambda: probes.owners_for_udc(targets.expected_udc)
    )
    if stock_ok and owners_ok:
        if stock_active:
            l4t_ok, l4t_udc = probe(
                "stock-udc", lambda: probes.read_text(targets.l4t_udc_path)
            )
            if l4t_ok:
                if (
                    str(l4t_udc).strip() == targets.expected_udc
                    and owners == frozenset({"l4t"})
                ):
                    verified.append("stock-already-correct")
                else:
                    errors.append(
                        CleanupError("stock-restore", "stock-active-wrong-state")
                    )
        elif not owners:
            command(reconciliation.stock_service_start)
        else:
            errors.append(CleanupError("stock-restore", "udc-not-free"))

    path_ok, hccast_exists = probe(
        "hccast-root-absent", lambda: probes.path_exists(targets.hccast_root)
    )
    mount_ok, mounted = probe(
        "functionfs-unmounted", lambda: probes.is_mountpoint(targets.functionfs_mountpoint)
    )
    final_owners_ok, final_owners = probe(
        "udc-owners", lambda: probes.owners_for_udc(targets.expected_udc)
    )
    final_stock_ok, final_stock = probe("stock-service-active", probes.stock_service_active)
    final_l4t_ok, final_l4t = probe(
        "stock-udc", lambda: probes.read_text(targets.l4t_udc_path)
    )

    checks: tuple[tuple[str, bool, bool], ...] = (
        ("hccast-root-absent", path_ok, not bool(hccast_exists)),
        ("functionfs-unmounted", mount_ok, not bool(mounted)),
        (
            "hccast-udc-released",
            final_owners_ok,
            "hccast" not in cast(frozenset[str], final_owners or frozenset()),
        ),
        ("stock-service-active", final_stock_ok, bool(final_stock)),
        (
            "stock-udc",
            final_l4t_ok,
            str(final_l4t).strip() == targets.expected_udc,
        ),
        (
            "stock-only-owner",
            final_owners_ok,
            final_owners == frozenset({"l4t"}),
        ),
    )
    for action, readable, satisfied in checks:
        if not readable:
            continue
        if satisfied:
            verified.append(action)
        else:
            errors.append(CleanupError(action, "postcondition not satisfied"))
    return CleanupResult(tuple(actions), tuple(errors), tuple(verified), not errors)
