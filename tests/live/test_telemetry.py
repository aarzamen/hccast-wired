"""Contract tests for the owned Jetson tegrastats recorder."""

from __future__ import annotations

from pathlib import Path
import signal
import stat
from typing import BinaryIO, Sequence

import pytest

from hccast_wired.live.telemetry import TelemetryRecorder


class FakeProcess:
    def __init__(self, *, returncode: int = 0, running: bool = True) -> None:
        self.pid = 4242
        self.returncode = returncode
        self.running = running
        self.wait_calls = 0

    def poll(self) -> int | None:
        return None if self.running else self.returncode

    def wait(self, timeout: float) -> int:
        assert timeout == 5.0
        self.wait_calls += 1
        self.running = False
        return self.returncode


class FakeLauncher:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.argv: tuple[str, ...] | None = None
        self.stdout: BinaryIO | None = None
        self.stop_calls = 0

    def launch(self, argv: Sequence[str], *, stdout: BinaryIO) -> FakeProcess:
        self.argv = tuple(argv)
        self.stdout = stdout
        return self.process

    def stop(self, process: FakeProcess, *, grace: float) -> int:
        assert process is self.process
        assert grace == 5.0
        self.stop_calls += 1
        if process.running:
            process.returncode = -signal.SIGTERM
        return process.wait(timeout=grace)


class RaisingLauncher(FakeLauncher):
    def launch(self, argv: Sequence[str], *, stdout: BinaryIO) -> FakeProcess:
        raise OSError("launch blocked")


def test_start_launches_five_second_tegrastats_into_private_log(tmp_path: Path) -> None:
    process = FakeProcess()
    launcher = FakeLauncher(process)

    recorder = TelemetryRecorder.start(tmp_path, launcher=launcher, token="unit")

    assert launcher.argv == ("/usr/bin/tegrastats", "--interval", "5000")
    assert recorder.log_path.name == "tegrastats-unit.log"
    assert stat.S_IMODE(recorder.log_path.stat().st_mode) == 0o600
    assert recorder.close().success is True


def test_close_terminates_waits_and_is_idempotent(tmp_path: Path) -> None:
    process = FakeProcess(running=True)
    launcher = FakeLauncher(process)
    recorder = TelemetryRecorder.start(tmp_path, launcher=launcher, token="owned")

    first = recorder.close()
    second = recorder.close()

    assert launcher.stop_calls == 1
    assert process.wait_calls == 1
    assert first == second


def test_nonzero_tegrastats_exit_is_a_failed_result(tmp_path: Path) -> None:
    process = FakeProcess(returncode=7, running=False)
    recorder = TelemetryRecorder.start(
        tmp_path,
        launcher=FakeLauncher(process),
        token="failed",
    )

    result = recorder.close()

    assert result.success is False
    assert result.returncode == 7
    assert result.error == "tegrastats exited 7"


def test_early_tegrastats_exit_is_reported_to_the_active_attempt(tmp_path: Path) -> None:
    process = FakeProcess(returncode=7, running=False)
    recorder = TelemetryRecorder.start(
        tmp_path,
        launcher=FakeLauncher(process),
        token="early",
    )

    assert recorder.poll_failure() == "telemetry-exited:7"
    recorder.close()


def test_relative_or_symlinked_evidence_root_is_rejected(tmp_path: Path) -> None:
    launcher = FakeLauncher(FakeProcess())
    with pytest.raises(ValueError, match="absolute"):
        TelemetryRecorder.start(Path("relative"), launcher=launcher, token="x")

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        TelemetryRecorder.start(link, launcher=launcher, token="x")


def test_launch_failure_closes_and_removes_the_empty_log(tmp_path: Path) -> None:
    launcher = RaisingLauncher(FakeProcess())

    with pytest.raises(OSError, match="launch blocked"):
        TelemetryRecorder.start(tmp_path, launcher=launcher, token="blocked")

    assert not (tmp_path / "tegrastats-blocked.log").exists()
