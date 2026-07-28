"""Local-only contract and regression tests for the live subprocess backend."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest

from hccast_wired.live.backend import (
    BackendTiming,
    ReconciliationTargets,
    SubprocessAttempt,
    SubprocessAttemptFactory,
    SubprocessLauncher,
    SystemProbe,
    _Child,
    _HandshakeParser,
    _pump_binary,
    _stop_child,
    _terminate_process_group,
)
from hccast_wired.live.commands import LiveCommandPlan, ProcessSpec, ReconciliationPlan
from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase
from hccast_wired.live.supervisor import (
    AttemptClassification,
    AttemptEvent,
    AttemptResult,
    CleanupError,
    CleanupResult,
)


PYTHON = str(Path(sys.executable).resolve())
HELPERS = Path(__file__).parent / "helpers"
TIMING = BackendTiming(
    x_readiness=0.8,
    handshake=1.5,
    one_shot=0.6,
    term_grace=0.25,
    kill_reap=0.25,
    poll_interval=0.005,
)


def _spec(
    name: str,
    *args: str,
    env: dict[str, str] | None = None,
    run_as_user: str | None = None,
) -> ProcessSpec:
    return ProcessSpec(
        name=name,
        argv=(PYTHON, *args),
        env=env or {"PATH": "/usr/bin:/bin"},
        run_as_user=run_as_user,
    )


class MutableProbe:
    def __init__(self) -> None:
        self.x_ready = False
        self.hccast_present = False
        self.mounted = False
        self.owners: frozenset[str] = frozenset({"l4t"})
        self.stock_active = True
        self.l4t_udc = "udc-test\n"
        self.raise_on: set[str] = set()

    def _raise(self, name: str) -> None:
        if name in self.raise_on:
            raise OSError(f"injected {name} failure")

    def path_exists(self, path: Path) -> bool:
        if path.name == "X99":
            self._raise("x-path")
            return self.x_ready
        self._raise("hccast-path")
        return self.hccast_present

    def is_mountpoint(self, path: Path) -> bool:
        self._raise("mountpoint")
        return self.mounted

    def read_text(self, path: Path) -> str:
        self._raise("l4t-udc")
        return self.l4t_udc

    def owners_for_udc(self, expected_udc: str) -> frozenset[str]:
        self._raise("owners")
        return self.owners

    def stock_service_active(self) -> bool:
        self._raise("stock-active")
        return self.stock_active


class RecordingLauncher:
    """Real local Popen launcher with mutable fake physical effects."""

    def __init__(self, probe: MutableProbe, *, uid: int = 501, user: str = "ama") -> None:
        self.probe = probe
        self.uid = uid
        self.user = user
        self.delegate = SubprocessLauncher()
        self.launches: list[tuple[tuple[str, ...], dict[str, str]]] = []
        self.commands: list[tuple[str, ...]] = []
        self.timeline: list[tuple[str, tuple[str, ...]]] = []
        self.processes: list[subprocess.Popen[bytes]] = []
        self.block_stock_stop = threading.Event()
        self.stock_stop_entered = threading.Event()
        self.interrupt_event: threading.Event | None = None
        self.interrupt_after_launch: int | None = None
        self.run_timings: list[tuple[float, float | None, float | None, float | None]] = []
        self.interrupt_timings: list[tuple[float, float, float, float]] = []

    @staticmethod
    def _assert_safe_executable(argv: tuple[str, ...]) -> None:
        assert Path(argv[0]).resolve() == Path(PYTHON)

    def launch(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        stdin: Any,
        stdout: Any,
        stderr: Any,
    ) -> subprocess.Popen[bytes]:
        self._assert_safe_executable(argv)
        self.launches.append((argv, dict(env)))
        self.timeline.append(("launch", argv))
        if (
            "--role" in argv
            and argv[argv.index("--role") + 1] == "xvfb"
            or any("role:xvfb" in argument for argument in argv)
        ):
            self.probe.x_ready = True
        process = self.delegate.launch(
            argv, env=env, stdin=stdin, stdout=stdout, stderr=stderr
        )
        self.processes.append(process)
        if (
            self.interrupt_event is not None
            and self.interrupt_after_launch == len(self.launches)
        ):
            self.interrupt_event.set()
        return process

    def _effect(self, argv: tuple[str, ...], code: int) -> None:
        joined = " ".join(argv)
        if code != 0:
            return
        if "stock-stop" in joined:
            self.probe.stock_active = False
            self.probe.owners = frozenset()
            self.probe.l4t_udc = ""
        elif "gadget-cleanup" in joined:
            self.probe.hccast_present = False
            self.probe.mounted = False
            self.probe.owners = frozenset(
                owner for owner in self.probe.owners if owner != "hccast"
            )
        elif "stock-start" in joined:
            self.probe.stock_active = True
            self.probe.owners = frozenset({"l4t"})
            self.probe.l4t_udc = "udc-test\n"

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        poll_interval: float | None = None,
        term_grace: float | None = None,
        kill_reap: float | None = None,
    ) -> int:
        self._assert_safe_executable(argv)
        self.commands.append(argv)
        self.timeline.append(("command", argv))
        self.run_timings.append((timeout, poll_interval, term_grace, kill_reap))
        code = self.delegate.run(
            argv,
            env=env,
            timeout=timeout,
            poll_interval=poll_interval or TIMING.poll_interval,
            term_grace=term_grace or TIMING.term_grace,
            kill_reap=kill_reap or TIMING.kill_reap,
        )
        self._effect(argv, code)
        return code

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
        self._assert_safe_executable(argv)
        self.commands.append(argv)
        self.timeline.append(("command", argv))
        self.interrupt_timings.append((timeout, poll_interval, term_grace, kill_reap))
        if "stock-stop" in " ".join(argv) and self.block_stock_stop.is_set():
            self.stock_stop_entered.set()
            while not interrupted.wait(poll_interval):
                pass
            return None
        code = self.delegate.run_interruptible(
            argv,
            env=env,
            timeout=timeout,
            interrupted=interrupted,
            poll_interval=poll_interval,
            term_grace=term_grace,
            kill_reap=kill_reap,
        )
        if code is not None:
            self._effect(argv, code)
        return code

    def effective_uid(self) -> int:
        return self.uid

    def current_user(self) -> str:
        return self.user

    def passwd(self, user: str) -> object:
        return SimpleNamespace(pw_uid=1001, pw_dir=f"/srv/home/{user}")

    def signal_group(self, pid: int, sig: signal.Signals) -> None:
        self.delegate.signal_group(pid, sig)

    def process_group_exists(self, pgid: int) -> bool:
        for process in self.processes:
            if process.pid == pgid:
                # The ordinary fake helpers never fork.  Using their owned direct
                # child state keeps this fixture deterministic under the macOS
                # sandbox, which may deny signal-0 probes after a group vanishes.
                return process.poll() is None
        return self.delegate.process_group_exists(pgid)

    def wait(self, process: subprocess.Popen[bytes], timeout: float) -> int:
        return self.delegate.wait(process, timeout)


def _command(name: str, exit_code: int = 0) -> ProcessSpec:
    return _spec(name, "-c", f"# {name}\nraise SystemExit({exit_code})")


def _source(tmp_path: Path, name: str, *, ignore_term: bool = False) -> ProcessSpec:
    argv = [
        str(HELPERS / "fake_encoder_process.py"),
        "--trace",
        str(tmp_path / "trace.log"),
        "--role",
        name,
    ]
    if ignore_term:
        argv.append("--ignore-term")
    return _spec(name, *argv)


def _plan(
    tmp_path: Path,
    *,
    payload: bytes = b"\x00\xff\nH264\x80",
    encoder: ProcessSpec | None = None,
    gadget: ProcessSpec | None = None,
    openbox: ProcessSpec | None = None,
    previews: bool = False,
    stock_stop_code: int = 0,
) -> LiveCommandPlan:
    trace = tmp_path / "trace.log"
    capture = tmp_path / "capture.bin"
    gadget = gadget or _spec(
        "gadget-stream",
        str(HELPERS / "fake_hccast_process.py"),
        "--trace",
        str(trace),
        "--capture",
        str(capture),
    )
    encoder = encoder or _spec(
        "encoder",
        str(HELPERS / "fake_encoder_process.py"),
        "--trace",
        str(trace),
        "--payload-hex",
        payload.hex(),
    )
    cleanup = _command("gadget-cleanup")
    stock_start = _command("stock-start")
    return LiveCommandPlan(
        xvfb=_source(tmp_path, "xvfb"),
        openbox=openbox or _source(tmp_path, "openbox"),
        chromium=_source(tmp_path, "chromium") if previews else None,
        x11vnc=_source(tmp_path, "x11vnc") if previews else None,
        websockify=_source(tmp_path, "websockify") if previews else None,
        encoder=encoder,
        gadget=gadget,
        gadget_cleanup=cleanup,
        stock_service_stop=_command("stock-stop", stock_stop_code),
        stock_service_start=stock_start,
    )


def _factory(
    tmp_path: Path,
    plan: LiveCommandPlan,
    probe: MutableProbe | None = None,
    launcher: RecordingLauncher | None = None,
    *,
    timing: BackendTiming = TIMING,
) -> tuple[SubprocessAttemptFactory, MutableProbe, RecordingLauncher]:
    probe = probe or MutableProbe()
    launcher = launcher or RecordingLauncher(probe)
    factory = SubprocessAttemptFactory(
        build_plan=lambda _config: plan,
        reconciliation=ReconciliationPlan(plan.gadget_cleanup, plan.stock_service_start),
        targets=ReconciliationTargets(
            expected_udc="udc-test",
            hccast_root=tmp_path / "configfs" / "hccast",
            functionfs_mountpoint=tmp_path / "ffs-hccast",
            l4t_udc_path=tmp_path / "configfs" / "l4t" / "UDC",
            x_socket_path=tmp_path / "X99",
        ),
        evidence_root=tmp_path / "evidence",
        launcher=launcher,
        probes=probe,
        timing=timing,
        utc_now=lambda: datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    return factory, probe, launcher


def _run(
    factory: SubprocessAttemptFactory, interrupted: threading.Event | None = None
) -> tuple[SubprocessAttempt, AttemptResult | None, list[AttemptEvent]]:
    events: list[AttemptEvent] = []
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    result = attempt.run(events.append, interrupted or threading.Event())
    return attempt, result, events


def _assert_reaped(launcher: RecordingLauncher) -> None:
    assert launcher.processes
    assert all(process.poll() is not None for process in launcher.processes)


def test_backend_defaults_and_factory_are_exposed() -> None:
    assert SubprocessAttemptFactory is not None
    assert BackendTiming() == BackendTiming(10.0, 125.0, 15.0, 3.0, 3.0, 0.05)


def test_create_rejects_stopped_even_if_builder_returns_active_plan(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    factory, _, launcher = _factory(tmp_path, plan)
    with pytest.raises(ValueError, match="stopped"):
        factory.create(LiveConfig(mode=DesiredMode.STOPPED))
    assert launcher.launches == []
    assert launcher.commands == []


def test_create_is_process_free_and_captures_injected_monotonic(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    launcher = RecordingLauncher(probe)
    factory = SubprocessAttemptFactory(
        build_plan=lambda _config: plan,
        reconciliation=ReconciliationPlan(plan.gadget_cleanup, plan.stock_service_start),
        targets=ReconciliationTargets("udc-test", x_socket_path=tmp_path / "X99"),
        evidence_root=tmp_path / "evidence",
        launcher=launcher,
        probes=probe,
        monotonic=lambda: 42.5,
    )
    attempt = factory.create(LiveConfig(mode=DesiredMode.DESKTOP))
    assert attempt.started_monotonic == 42.5
    assert launcher.launches == [] and launcher.commands == []


def test_root_user_switch_vector_and_replacement_environment(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    launcher = RecordingLauncher(probe, uid=0, user="root")
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    spec = _spec("openbox", "-c", "pass", env={"SECRET": "dropped"}, run_as_user="alice")
    argv, env = attempt._argv_and_env(spec)
    assert argv[:6] == (
        "/usr/sbin/runuser",
        "--user",
        "alice",
        "--",
        "/usr/bin/env",
        "--ignore-environment",
    )
    assert "HOME=/srv/home/alice" in argv
    assert env == {
        "HOME": "/srv/home/alice",
        "USER": "alice",
        "LOGNAME": "alice",
        "PATH": "/usr/bin:/bin",
        "DISPLAY": ":99",
        "XDG_RUNTIME_DIR": "/run/user/1001",
    }
    assert "SECRET" not in env


def test_non_root_cross_user_fails_closed_and_gadget_env_is_unbuffered(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    with pytest.raises(PermissionError, match="impersonate"):
        attempt._argv_and_env(_spec("openbox", "-c", "pass", run_as_user="other"))
    argv, env = attempt._argv_and_env(plan.gadget)
    assert argv[0] == PYTHON
    assert "-u" not in argv
    assert env["PYTHONUNBUFFERED"] == "1"


class _FakePopen:
    instances: list[_FakePopen] = []

    def __init__(self, argv: tuple[str, ...], **kwargs: Any) -> None:
        self.argv = argv
        self.kwargs = kwargs
        self.pid = 999999
        self.returncode = 0
        self.__class__.instances.append(self)

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0


def test_production_launcher_isolates_long_lived_and_one_shot_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakePopen.instances.clear()
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    launcher = SubprocessLauncher(process_group_exists=lambda _pgid: False)
    launcher.launch((PYTHON, "-c", "pass"), env={}, stdin=None, stdout=None, stderr=None)
    assert (
        launcher.run(
            (PYTHON, "-c", "pass"),
            env={},
            timeout=0.1,
            poll_interval=0.005,
            term_grace=0.01,
            kill_reap=0.01,
        )
        == 0
    )
    assert len(_FakePopen.instances) == 2
    for process in _FakePopen.instances:
        assert process.kwargs["shell"] is False
        assert process.kwargs["close_fds"] is True
        assert process.kwargs["start_new_session"] is True
        assert process.kwargs["env"] == {}


class _ProbeLauncher:
    def __init__(self, code: int) -> None:
        self.code = code
        self.calls: list[tuple[tuple[str, ...], float, float | None, float | None, float | None]] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        env: dict[str, str],
        timeout: float,
        poll_interval: float | None = None,
        term_grace: float | None = None,
        kill_reap: float | None = None,
    ) -> int:
        self.calls.append((argv, timeout, poll_interval, term_grace, kill_reap))
        return self.code


def test_system_probe_uses_launcher_for_stock_truth_and_discovers_all_gadgets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "usb_gadget"
    for name, udc in (("hccast", "other"), ("l4t", "udc-test"), ("third", "udc-test")):
        path = root / name
        path.mkdir(parents=True)
        (path / "UDC").write_text(udc + "\n", encoding="utf-8")
    launcher = _ProbeLauncher(0)
    probe = SystemProbe(launcher=launcher, configfs_gadget_root=root)
    assert probe.stock_service_active()
    assert probe.owners_for_udc("udc-test") == frozenset({"l4t", "third"})
    assert launcher.calls[0][0] == (
        "/usr/bin/systemctl",
        "is-active",
        "--quiet",
        "nv-l4t-usb-device-mode.service",
    )


def test_system_probe_configfs_enumeration_race_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "usb_gadget"
    (root / "disappearing").mkdir(parents=True)
    probe = SystemProbe(launcher=_ProbeLauncher(0), configfs_gadget_root=root)
    with pytest.raises(FileNotFoundError):
        probe.owners_for_udc("udc-test")


def test_evidence_start_failure_has_no_process_or_command_side_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, launcher = _factory(tmp_path, plan)

    def reject(*args: Any, **kwargs: Any) -> None:
        raise ValueError("injected evidence failure")

    monkeypatch.setattr("hccast_wired.live.backend.RunEvidenceWriter.start", reject)
    attempt, result, _ = _run(factory)
    assert result is not None
    assert result.error == "startup-failed: injected evidence failure"
    assert result.cleanup.success and result.cleanup.attempted_actions == ()
    assert attempt.stop() is result.cleanup
    assert launcher.launches == [] and launcher.commands == []


def test_stop_before_run_is_terminal_and_never_launches_work(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    factory, _, launcher = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    cleanup = attempt.stop()
    assert cleanup.success and cleanup.attempted_actions == ()
    assert attempt.run(lambda _event: None, threading.Event()) is None
    assert attempt.stop() is cleanup
    assert launcher.launches == [] and launcher.commands == []


@pytest.mark.parametrize(
    ("chunks", "expected"),
    [
        ([b"noise HCCAST hand", b"shake complete:\n{\"product\":\"P\",", b"\"version\":\"V\"}"], ("P", "V")),
        ([b"HCCAST handshake complete:\n{\n  \"product\": \"P\",\n", b"  \"version\": \"V\"\n}\n"], ("P", "V")),
    ],
)
def test_handshake_parser_handles_fragmented_marker_and_json(
    chunks: list[bytes], expected: tuple[str, str]
) -> None:
    parser = _HandshakeParser(limit=65536)
    for chunk in chunks:
        parser.feed_stdout(chunk)
    assert parser.outcome == expected
    assert parser.buffered_bytes <= 65536


def test_handshake_parser_bounds_pre_marker_and_exact_json_limit() -> None:
    parser = _HandshakeParser(limit=65536)
    parser.feed_stdout(b"x" * 65537)
    assert parser.error == "handshake-json-too-large"
    assert parser.buffered_bytes <= 65536

    value = b'{"product":"P","version":"V","padding":"' + b"x" * 10 + b'"}'
    exact = value + b" " * (65536 - len(value))
    parser = _HandshakeParser(limit=65536)
    parser.feed_stdout(b"HCCAST handshake complete:" + exact)
    assert parser.outcome == ("P", "V")

    parser = _HandshakeParser(limit=65536)
    parser.feed_stdout(b"HCCAST handshake complete:" + exact + b"x")
    assert parser.error == "handshake-json-too-large"


@pytest.mark.parametrize(
    ("payload", "eof", "expected"),
    [
        (b'{"product":"P","version":}', False, "handshake-json"),
        (b"", True, "handshake-json-incomplete"),
        (b'{"product":"P"', True, "handshake-json-incomplete"),
        (b'{"product":1,"version":"V"}', False, "handshake-json"),
    ],
)
def test_handshake_parser_classifies_malformed_incomplete_eof_and_fields(
    payload: bytes, eof: bool, expected: str
) -> None:
    parser = _HandshakeParser(limit=65536)
    parser.feed_stdout(b"HCCAST handshake complete:" + payload)
    if eof:
        parser.stdout_eof()
    assert parser.error == expected


class _ShortWriter(io.BytesIO):
    def write(self, data: Any) -> int:
        return super().write(data[:2])


class _BrokenWriter(io.BytesIO):
    def write(self, data: Any) -> int:
        raise BrokenPipeError("closed")


def test_binary_pump_preserves_short_writes_and_signals_failures() -> None:
    writer = _ShortWriter()
    assert _pump_binary(io.BytesIO(b"abcdef"), writer, lambda: None) == "encoder-eof"
    assert writer.getvalue() == b"abcdef"
    assert _pump_binary(io.BytesIO(b"abc"), _BrokenWriter(), lambda: None) == "stream-pipe"


def test_local_lifecycle_orders_startup_logs_pipes_bytes_and_final_reason(tmp_path: Path) -> None:
    payload = b"\x00\xff\nH264\x80"
    plan = _plan(tmp_path, payload=payload, previews=True)
    factory, _, launcher = _factory(tmp_path, plan)
    attempt, result, events = _run(factory)
    assert result is not None
    assert result.classification is AttemptClassification.FAILURE
    assert result.error == "encoder-exited"
    assert result.cleanup.success
    assert [(event.phase, event.product, event.version) for event in events] == [
        (RuntimePhase.WAITING_FOR_SCREEN, None, None),
        (RuntimePhase.HANDSHAKING, None, None),
        (RuntimePhase.STREAMING, "HCT-AT01", "2505161526"),
    ]
    assert (tmp_path / "capture.bin").read_bytes() == payload
    evidence = next((tmp_path / "evidence").iterdir())
    assert "encoder diagnostic" in (evidence / "encoder.log").read_text(encoding="utf-8")
    source_log = (evidence / "source.log").read_text(encoding="utf-8")
    for name in ("xvfb", "openbox", "x11vnc", "websockify", "chromium"):
        assert f"[{name}][stderr]" in source_log
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "encoder-exited"
    assert document["cleanup"]["success"] is True
    assert attempt.stop() is result.cleanup
    _assert_reaped(launcher)
    timeline = [(kind, " ".join(argv)) for kind, argv in launcher.timeline]
    positions = {
        name: next(index for index, (_kind, argv) in enumerate(timeline) if name in argv)
        for name in (
            "--role xvfb",
            "--role openbox",
            "--role x11vnc",
            "--role websockify",
            "--role chromium",
            "stock-stop",
            "fake_hccast_process.py",
            "--payload-hex",
        )
    }
    assert list(positions.values()) == sorted(positions.values())
    for argv, _env in launcher.launches:
        executable = Path(argv[0]).resolve()
        assert executable == Path(PYTHON) or executable.is_relative_to(tmp_path)


def test_source_and_encoder_pressure_is_continuously_drained_and_logged(tmp_path: Path) -> None:
    pressure = "x" * 180_000
    openbox = _spec(
        "openbox",
        "-c",
        f"import sys,time; print({pressure!r}); print({pressure!r},file=sys.stderr); time.sleep(60)",
    )
    encoder = _spec(
        "encoder",
        "-c",
        f"import sys,time; print({pressure!r},file=sys.stderr); sys.stderr.flush(); "
        "sys.stdout.buffer.write(b'abc'); sys.stdout.buffer.flush(); time.sleep(60)",
    )
    plan = _plan(tmp_path, openbox=openbox, encoder=encoder)
    factory, _, launcher = _factory(tmp_path, plan)
    interrupted = threading.Event()
    outcome: list[object] = []
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    thread = threading.Thread(target=lambda: outcome.append(attempt.run(lambda _e: None, interrupted)))
    thread.start()
    deadline = time.monotonic() + 3
    while attempt._streaming_at is None and time.monotonic() < deadline:
        time.sleep(0.01)
    evidence = next((tmp_path / "evidence").iterdir())
    encoder_log = evidence / "encoder.log"
    while (
        (not encoder_log.exists() or encoder_log.stat().st_size < len(pressure))
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)
    interrupted.set()
    thread.join(3)
    assert not thread.is_alive() and outcome == [None]
    assert (evidence / "source.log").stat().st_size >= len(pressure) * 2
    assert (evidence / "encoder.log").stat().st_size >= len(pressure)
    _assert_reaped(launcher)


def test_source_exit_during_startup_or_streaming_is_retryable(tmp_path: Path) -> None:
    openbox = _spec("openbox", "-c", "print('source done')")
    plan = _plan(tmp_path, openbox=openbox, encoder=_source(tmp_path, "encoder"))
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "source-exited"
    _assert_reaped(launcher)


def test_encoder_stdout_eof_while_process_alive_is_retryable(tmp_path: Path) -> None:
    encoder = _spec(
        "encoder",
        "-c",
        "import os,time; os.close(1); time.sleep(60)",
    )
    plan = _plan(tmp_path, encoder=encoder)
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "encoder-eof"
    _assert_reaped(launcher)


def test_broken_gadget_stdin_reaches_stream_pipe_failure(tmp_path: Path) -> None:
    gadget_code = (
        "import os,sys,time; "
        "print('Enumerating directly as Android Open Accessory 18d1:2d00...'); "
        "print('TX SETR device-info request',file=sys.stderr); "
        "print('HCCAST handshake complete:'); "
        "print('{\"product\":\"P\",\"version\":\"V\"}'); "
        "sys.stdout.flush(); sys.stderr.flush(); os.close(0); time.sleep(60)"
    )
    gadget = _spec("gadget-stream", "-c", gadget_code)
    encoder = _spec(
        "encoder",
        str(HELPERS / "fake_encoder_process.py"),
        "--mode",
        "repeat",
        "--payload-hex",
        (b"x" * 8192).hex(),
    )
    plan = _plan(tmp_path, gadget=gadget, encoder=encoder)
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "stream-pipe"
    _assert_reaped(launcher)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("malformed-json", "handshake-json"),
        ("oversized-json", "handshake-json-too-large"),
    ],
)
def test_real_gadget_malformed_and_oversized_modes_are_bounded(
    tmp_path: Path, mode: str, expected: str
) -> None:
    gadget = _spec(
        "gadget-stream",
        str(HELPERS / "fake_hccast_process.py"),
        "--mode",
        mode,
    )
    plan = _plan(tmp_path, gadget=gadget)
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == expected
    _assert_reaped(launcher)


def test_marker_only_stdout_eof_is_incomplete_handshake_json(tmp_path: Path) -> None:
    code = (
        "import os,sys,time; print('Enumerating directly as Android Open Accessory'); "
        "print('TX SETR device-info request',file=sys.stderr); "
        "print('HCCAST handshake complete:'); sys.stdout.flush(); sys.stderr.flush(); "
        "os.close(1); time.sleep(60)"
    )
    plan = _plan(tmp_path, gadget=_spec("gadget-stream", "-c", code))
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "handshake-json-incomplete"
    _assert_reaped(launcher)


def test_gadget_stderr_pressure_does_not_deadlock_json_and_is_logged(tmp_path: Path) -> None:
    pressure = "z" * 180_000
    code = (
        "import sys; "
        f"sys.stderr.write({pressure!r}); sys.stderr.write('\\nTX SETR device-info request\\n'); "
        "sys.stderr.flush(); print('Enumerating directly as Android Open Accessory'); "
        "print('HCCAST handshake complete:'); "
        "print('{\"product\":\"P\",\"version\":\"V\"}'); sys.stdout.flush(); "
        "sys.stdin.buffer.read()"
    )
    plan = _plan(tmp_path, gadget=_spec("gadget-stream", "-c", code))
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, events = _run(factory)
    assert result is not None and result.error == "encoder-exited"
    assert [event.phase for event in events] == [
        RuntimePhase.WAITING_FOR_SCREEN,
        RuntimePhase.HANDSHAKING,
        RuntimePhase.STREAMING,
    ]
    evidence = next((tmp_path / "evidence").iterdir())
    assert (evidence / "gadget.log").stat().st_size >= len(pressure)
    _assert_reaped(launcher)


def test_gadget_drain_continues_logging_after_handshake_without_queue(tmp_path: Path) -> None:
    after = "after-handshake-" * 10_000
    code = (
        "import sys; print('Enumerating directly as Android Open Accessory'); "
        "print('TX SETR device-info request',file=sys.stderr); "
        "print('HCCAST handshake complete:'); "
        "print('{\"product\":\"P\",\"version\":\"V\"}'); "
        f"print({after!r}); sys.stdout.flush(); sys.stderr.flush(); sys.stdin.buffer.read()"
    )
    encoder = _source(tmp_path, "encoder")
    plan = _plan(tmp_path, gadget=_spec("gadget-stream", "-c", code), encoder=encoder)
    factory, _, launcher = _factory(tmp_path, plan)
    interrupted = threading.Event()
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    outcome: list[object] = []
    thread = threading.Thread(target=lambda: outcome.append(attempt.run(lambda _e: None, interrupted)))
    thread.start()
    deadline = time.monotonic() + 3
    evidence_root = tmp_path / "evidence"
    gadget_log: Path | None = None
    while time.monotonic() < deadline:
        if evidence_root.exists():
            entries = list(evidence_root.iterdir())
            if entries:
                gadget_log = entries[0] / "gadget.log"
                if gadget_log.exists() and gadget_log.stat().st_size >= len(after):
                    break
        time.sleep(0.01)
    interrupted.set()
    thread.join(3)
    assert not thread.is_alive() and outcome == [None]
    assert gadget_log is not None and gadget_log.stat().st_size >= len(after)
    _assert_reaped(launcher)


@pytest.mark.parametrize("source_name", ["xvfb", "openbox", "x11vnc", "websockify", "chromium"])
def test_every_owned_source_exit_during_streaming_is_detected(
    tmp_path: Path, source_name: str
) -> None:
    delayed_exit = _spec(
        source_name,
        "-c",
        f"# role:{source_name}\nimport time; time.sleep(0.25)",
    )
    plan = _plan(tmp_path, encoder=_source(tmp_path, "encoder"), previews=True)
    values = {
        "xvfb": plan.xvfb,
        "openbox": plan.openbox,
        "x11vnc": plan.x11vnc,
        "websockify": plan.websockify,
        "chromium": plan.chromium,
    }
    values[source_name] = delayed_exit
    plan = LiveCommandPlan(
        xvfb=cast(ProcessSpec, values["xvfb"]),
        openbox=cast(ProcessSpec, values["openbox"]),
        chromium=cast(ProcessSpec, values["chromium"]),
        x11vnc=cast(ProcessSpec, values["x11vnc"]),
        websockify=cast(ProcessSpec, values["websockify"]),
        encoder=plan.encoder,
        gadget=plan.gadget,
        gadget_cleanup=plan.gadget_cleanup,
        stock_service_stop=plan.stock_service_stop,
        stock_service_start=plan.stock_service_start,
    )
    factory, _, launcher = _factory(tmp_path, plan)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "source-exited"
    _assert_reaped(launcher)


def test_stock_stop_failure_and_retained_udc_ownership_prevent_gadget_launch(
    tmp_path: Path,
) -> None:
    failed = _plan(tmp_path / "failed", stock_stop_code=9)
    factory, _, launcher = _factory(tmp_path / "failed", failed)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "stock-stop-failed"
    assert not any("fake_hccast_process.py" in " ".join(argv) for argv, _ in launcher.launches)

    retained_path = tmp_path / "retained"
    retained = _plan(retained_path)
    probe = MutableProbe()
    launcher = RecordingLauncher(probe)

    def retain(argv: tuple[str, ...], code: int) -> None:
        if "stock-stop" in " ".join(argv) and code == 0:
            probe.stock_active = False
            probe.owners = frozenset({"third"})

    launcher._effect = retain  # type: ignore[method-assign]
    factory, _, launcher = _factory(retained_path, retained, probe, launcher)
    _, result, _ = _run(factory)
    assert result is not None and result.error == "udc-not-free"
    assert not any("fake_hccast_process.py" in " ".join(argv) for argv, _ in launcher.launches)


def test_interruption_before_startup_launches_no_child_and_records_full_result(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.raise_on.add("stock-active")
    factory, _, launcher = _factory(tmp_path, plan, probe)
    interrupted = threading.Event()
    interrupted.set()
    attempt, result, _ = _run(factory, interrupted)
    assert result is None
    cleanup = attempt.stop()
    assert not cleanup.success
    assert launcher.launches == []
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["classification"] == "completed"
    assert document["terminal_reason"] == "interrupted"
    assert document["error"] is None
    assert document["streaming_duration"] == 0.0
    assert document["cleanup"]["success"] is False


def test_stock_stop_is_interruptible_and_no_gadget_launch_follows_cancel(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    launcher = RecordingLauncher(probe)
    launcher.block_stock_stop.set()
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    interrupted = threading.Event()
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    outcome: list[object] = []
    thread = threading.Thread(target=lambda: outcome.append(attempt.run(lambda _e: None, interrupted)))
    thread.start()
    assert launcher.stock_stop_entered.wait(2)
    interrupted.set()
    thread.join(3)
    assert not thread.is_alive() and outcome == [None]
    assert not any("fake_hccast_process.py" in " ".join(argv) for argv, _ in launcher.launches)
    _assert_reaped(launcher)


@pytest.mark.parametrize("interrupt_after_launch", range(1, 8))
def test_interruption_between_each_startup_launch_prevents_all_later_work(
    tmp_path: Path, interrupt_after_launch: int
) -> None:
    plan = _plan(tmp_path, encoder=_source(tmp_path, "encoder"), previews=True)
    probe = MutableProbe()
    launcher = RecordingLauncher(probe)
    interrupted = threading.Event()
    launcher.interrupt_event = interrupted
    launcher.interrupt_after_launch = interrupt_after_launch
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    attempt, result, _ = _run(factory, interrupted)
    assert result is None
    assert len(launcher.launches) == interrupt_after_launch
    cleanup = attempt.stop()
    assert cleanup.success, cleanup.errors
    _assert_reaped(launcher)


def test_cleanup_probe_exception_settles_one_cached_result_and_finalizes_evidence(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.x_ready = True
    probe.raise_on.add("stock-active")
    factory, _, _ = _factory(tmp_path, plan, probe)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    run_results: list[object] = []
    thread = threading.Thread(
        target=lambda: run_results.append(attempt.run(lambda _event: None, threading.Event()))
    )
    thread.start()
    thread.join(3)
    assert not thread.is_alive()
    results: list[CleanupResult] = []
    stops = [threading.Thread(target=lambda: results.append(attempt.stop())) for _ in range(8)]
    for stop in stops:
        stop.start()
    for stop in stops:
        stop.join(1)
    assert all(not stop.is_alive() for stop in stops)
    assert len(results) == 8 and all(result is results[0] for result in results)
    assert not results[0].success
    evidence = next((tmp_path / "evidence").iterdir())
    assert (evidence / "RESULT.json").is_file()


def test_unexpected_cleanup_exception_is_contained_cached_and_never_deadlocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    monkeypatch.setattr(attempt, "_active_lifecycle", lambda _publish, _interrupted: "forced")

    def explode() -> CleanupResult:
        raise RuntimeError("injected cleanup explosion")

    monkeypatch.setattr(attempt, "_cleanup", explode)
    result = attempt.run(lambda _event: None, threading.Event())
    assert result is not None
    assert not result.cleanup.success
    assert result.cleanup.errors == (
        CleanupError("cleanup-internal", "injected cleanup explosion"),
    )
    cached: list[CleanupResult] = []
    threads = [threading.Thread(target=lambda: cached.append(attempt.stop())) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
    assert all(not thread.is_alive() for thread in threads)
    assert len(cached) == 8 and all(cleanup is result.cleanup for cleanup in cached)
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["cleanup"]["success"] is False


@pytest.mark.parametrize(
    ("stock_active", "owners", "l4t", "start_expected", "success", "error_action"),
    [
        (True, frozenset({"l4t"}), "udc-test\n", False, True, None),
        (False, frozenset(), "", True, True, None),
        (False, frozenset({"third"}), "", False, False, "stock-restore"),
        (True, frozenset({"third"}), "other", False, False, "stock-restore"),
    ],
)
def test_reconciliation_stock_state_matrix(
    tmp_path: Path,
    stock_active: bool,
    owners: frozenset[str],
    l4t: str,
    start_expected: bool,
    success: bool,
    error_action: str | None,
) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.stock_active, probe.owners, probe.l4t_udc = stock_active, owners, l4t
    factory, _, launcher = _factory(tmp_path, plan, probe)
    result = factory.reconcile_stopped()
    assert result.success is success
    assert any("stock-start" in " ".join(argv) for argv in launcher.commands) is start_expected
    if error_action is not None:
        assert any(error.action == error_action for error in result.errors)


def test_reconciliation_accumulates_command_probe_and_all_postcondition_failures(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    plan = LiveCommandPlan(
        plan.xvfb,
        plan.openbox,
        plan.chromium,
        plan.x11vnc,
        plan.websockify,
        plan.encoder,
        plan.gadget,
        _command("gadget-cleanup", 7),
        plan.stock_service_stop,
        plan.stock_service_start,
    )
    probe = MutableProbe()
    probe.hccast_present = True
    probe.mounted = True
    probe.stock_active = False
    probe.owners = frozenset({"hccast", "third"})
    probe.l4t_udc = "wrong"
    factory, _, _ = _factory(tmp_path, plan, probe)
    result = factory.reconcile_stopped()
    assert not result.success
    assert [error.action for error in result.errors] == [
        "gadget-cleanup",
        "stock-restore",
        "hccast-root-absent",
        "functionfs-unmounted",
        "hccast-udc-released",
        "stock-service-active",
        "stock-udc",
        "stock-only-owner",
    ]


@pytest.mark.parametrize(
    ("raise_on", "action"),
    [
        ("hccast-path", "hccast-root-absent"),
        ("mountpoint", "functionfs-unmounted"),
        ("owners", "udc-owners"),
        ("stock-active", "stock-service-active"),
        ("l4t-udc", "stock-udc"),
    ],
)
def test_each_reconciliation_probe_exception_is_bounded_and_reported(
    tmp_path: Path, raise_on: str, action: str
) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.raise_on.add(raise_on)
    factory, _, _ = _factory(tmp_path, plan, probe)
    result = factory.reconcile_stopped()
    assert not result.success
    assert any(error.action == action for error in result.errors)


def test_reconciliation_is_fresh_and_cleanup_command_failure_is_preserved(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    bad_cleanup = _command("gadget-cleanup", 7)
    plan = LiveCommandPlan(
        plan.xvfb,
        plan.openbox,
        plan.chromium,
        plan.x11vnc,
        plan.websockify,
        plan.encoder,
        plan.gadget,
        bad_cleanup,
        plan.stock_service_stop,
        plan.stock_service_start,
    )
    probe = MutableProbe()
    factory, _, _ = _factory(tmp_path, plan, probe)
    first = factory.reconcile_stopped()
    assert not first.success and first.errors[0] == CleanupError("gadget-cleanup", "exit 7")
    probe.stock_active = False
    probe.owners = frozenset({"third"})
    later = factory.reconcile_stopped()
    assert later is not first
    assert any(error.action == "stock-restore" for error in later.errors)


def test_term_resistant_child_is_killed_reaped_and_workers_join(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    plan = LiveCommandPlan(
        xvfb=_source(tmp_path, "xvfb", ignore_term=True),
        openbox=plan.openbox,
        chromium=plan.chromium,
        x11vnc=plan.x11vnc,
        websockify=plan.websockify,
        encoder=plan.encoder,
        gadget=plan.gadget,
        gadget_cleanup=plan.gadget_cleanup,
        stock_service_stop=plan.stock_service_stop,
        stock_service_start=plan.stock_service_start,
    )
    factory, _, launcher = _factory(tmp_path, plan)
    interrupted = threading.Event()
    interrupted.set()
    # A pre-set generation interrupt is caught before launch. Clear it only after run begins.
    interrupted.clear()
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    outcome: list[object] = []
    thread = threading.Thread(target=lambda: outcome.append(attempt.run(lambda _e: None, interrupted)))
    thread.start()
    deadline = time.monotonic() + 2
    while not launcher.processes and time.monotonic() < deadline:
        time.sleep(0.005)
    interrupted.set()
    thread.join(3)
    assert not thread.is_alive() and outcome == [None]
    _assert_reaped(launcher)
    assert all(not worker.is_alive() for worker in attempt._workers)


def test_concurrent_stop_during_streaming_is_identical_and_launches_nothing_after_cancel(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path, encoder=_source(tmp_path, "encoder"))
    factory, _, launcher = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    outcome: list[object] = []
    run_thread = threading.Thread(
        target=lambda: outcome.append(attempt.run(lambda _event: None, threading.Event()))
    )
    run_thread.start()
    deadline = time.monotonic() + 3
    while attempt._streaming_at is None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert attempt._streaming_at is not None
    time.sleep(0.02)
    launch_count = len(launcher.launches)
    results: list[CleanupResult] = []
    stops = [threading.Thread(target=lambda: results.append(attempt.stop())) for _ in range(8)]
    for stop in stops:
        stop.start()
    for stop in stops:
        stop.join(3)
    run_thread.join(3)
    assert not run_thread.is_alive() and outcome == [None]
    assert len(results) == 8 and all(result is results[0] for result in results)
    assert len(launcher.launches) == launch_count
    assert attempt.stop() is results[0]
    _assert_reaped(launcher)
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "interrupted"
    assert document["product"] == "HCT-AT01"
    assert document["version"] == "2505161526"
    assert document["streaming_duration"] > 0


def test_one_shot_timeout_kills_owned_descendant_group(tmp_path: Path) -> None:
    child_pid = tmp_path / "child.pid"
    code = (
        "import os,time,pathlib; pid=os.fork(); "
        f"path=pathlib.Path({str(child_pid)!r}); "
        "(path.write_text(str(os.getpid())), time.sleep(60)) if pid == 0 else time.sleep(60)"
    )
    def descendant_group_exists(pgid: int) -> bool:
        if not child_pid.is_file():
            return True
        try:
            return os.getpgid(int(child_pid.read_text(encoding="utf-8"))) == pgid
        except ProcessLookupError:
            return False

    launcher = SubprocessLauncher(process_group_exists=descendant_group_exists)
    with pytest.raises(subprocess.TimeoutExpired):
        launcher.run(
            (PYTHON, "-c", code),
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.15,
            poll_interval=0.005,
            term_grace=0.05,
            kill_reap=0.1,
        )
    deadline = time.monotonic() + 2
    while not child_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert child_pid.exists()
    pid = int(child_pid.read_text(encoding="utf-8"))
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    else:
        pytest.fail("owned one-shot descendant remained after timeout cleanup")


class _LateMutatingStockStopLauncher(RecordingLauncher):
    """Models a stock-stop command whose physical mutation lands after cancel."""

    def __init__(self, probe: MutableProbe) -> None:
        super().__init__(probe)
        self.interruption_seen = threading.Event()
        self.release_late_mutation = threading.Event()
        self.late_mutation_applied = threading.Event()

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
        if "stock-stop" not in " ".join(argv):
            return super().run_interruptible(
                argv,
                env=env,
                timeout=timeout,
                interrupted=interrupted,
                poll_interval=poll_interval,
                term_grace=term_grace,
                kill_reap=kill_reap,
            )
        self.commands.append(argv)
        self.timeline.append(("command", argv))
        self.interrupt_timings.append((timeout, poll_interval, term_grace, kill_reap))
        self.stock_stop_entered.set()
        assert interrupted.wait(2), "concurrent stop never interrupted stock-stop"
        self.interruption_seen.set()
        assert self.release_late_mutation.wait(2), "test did not release stock-stop"
        self.probe.stock_active = False
        self.probe.owners = frozenset()
        self.probe.l4t_udc = ""
        self.late_mutation_applied.set()
        return None


def test_external_stop_waits_for_active_mutation_before_cleanup_and_return(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    launcher = _LateMutatingStockStopLauncher(probe)
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    run_out: list[object] = []
    stop_out: list[CleanupResult] = []
    run_thread = threading.Thread(
        target=lambda: run_out.append(attempt.run(lambda _event: None, threading.Event()))
    )
    run_thread.start()
    assert launcher.stock_stop_entered.wait(2)
    stop_done = threading.Event()

    def stop_attempt() -> None:
        stop_out.append(attempt.stop())
        stop_done.set()

    stop_thread = threading.Thread(target=stop_attempt)
    stop_thread.start()
    assert launcher.interruption_seen.wait(2)
    returned_before_mutation_quiesced = stop_done.wait(0.15)
    launcher.release_late_mutation.set()
    run_thread.join(3)
    stop_thread.join(3)
    assert not returned_before_mutation_quiesced
    assert not run_thread.is_alive() and not stop_thread.is_alive()
    assert launcher.late_mutation_applied.is_set()
    assert run_out == [None]
    assert len(stop_out) == 1 and stop_out[0].success
    assert probe.stock_active
    assert probe.l4t_udc.strip() == "udc-test"
    assert probe.owners == frozenset({"l4t"})
    assert attempt.stop() is stop_out[0]
    _assert_reaped(launcher)


def test_external_stop_cannot_finish_before_post_launch_drainers_are_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, launcher = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    original_start = attempt._start
    child_registered = threading.Event()
    release_start = threading.Event()

    def blocked_start(*args: Any, **kwargs: Any) -> Any:
        child = original_start(*args, **kwargs)
        if child is not None and child.name == "xvfb":
            child_registered.set()
            assert release_start.wait(2), "test did not release post-launch boundary"
        return child

    monkeypatch.setattr(attempt, "_start", blocked_start)
    run_out: list[object] = []
    stop_out: list[CleanupResult] = []
    run_thread = threading.Thread(
        target=lambda: run_out.append(attempt.run(lambda _event: None, threading.Event()))
    )
    run_thread.start()
    assert child_registered.wait(2)
    stop_done = threading.Event()
    workers_when_stop_returned: list[tuple[threading.Thread, ...]] = []

    def stop_attempt() -> None:
        stop_out.append(attempt.stop())
        workers_when_stop_returned.append(tuple(attempt._workers))
        stop_done.set()

    stop_thread = threading.Thread(target=stop_attempt)
    stop_thread.start()
    returned_before_registration_completed = stop_done.wait(0.15)
    release_start.set()
    run_thread.join(3)
    stop_thread.join(3)
    assert not returned_before_registration_completed
    assert not run_thread.is_alive() and not stop_thread.is_alive()
    assert run_out == [None] and len(stop_out) == 1
    assert workers_when_stop_returned and workers_when_stop_returned[0]
    assert all(not worker.is_alive() for worker in workers_when_stop_returned[0])
    assert all(not worker.is_alive() for worker in attempt._workers)
    assert attempt.stop() is stop_out[0]
    _assert_reaped(launcher)


def test_run_and_stop_before_run_race_has_one_atomic_winner(tmp_path: Path) -> None:
    for iteration in range(20):
        case = tmp_path / str(iteration)
        plan = _plan(case)
        factory, _, launcher = _factory(case, plan)
        attempt = cast(
            SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP))
        )
        gate = threading.Barrier(3)
        run_out: list[object] = []
        stop_out: list[CleanupResult] = []

        def run_attempt() -> None:
            gate.wait()
            run_out.append(attempt.run(lambda _e: None, threading.Event()))

        def stop_attempt() -> None:
            gate.wait()
            stop_out.append(attempt.stop())

        run_thread = threading.Thread(target=run_attempt)
        stop_thread = threading.Thread(target=stop_attempt)
        run_thread.start()
        stop_thread.start()
        gate.wait()
        run_thread.join(3)
        stop_thread.join(3)
        assert not run_thread.is_alive() and not stop_thread.is_alive()
        assert len(run_out) == 1 and len(stop_out) == 1
        assert attempt.stop() is stop_out[0]
        if launcher.launches or launcher.commands:
            assert (case / "evidence").is_dir()
            if launcher.processes:
                _assert_reaped(launcher)
        else:
            assert not (case / "evidence").exists()
            assert stop_out[0].success


def test_preset_interrupt_does_not_invoke_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def counted_popen(argv: tuple[str, ...], **kwargs: Any) -> _FakePopen:
        calls.append(argv)
        return _FakePopen(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counted_popen)
    interrupted = threading.Event()
    interrupted.set()
    result = SubprocessLauncher().run_interruptible(
        (PYTHON, "-c", "pass"),
        env={},
        timeout=0.1,
        interrupted=interrupted,
        poll_interval=0.005,
        term_grace=0.01,
        kill_reap=0.01,
    )
    assert result is None
    assert calls == []


def test_interrupt_immediately_after_launch_reaps_child_and_quiesces_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_popen = subprocess.Popen
    interrupted = threading.Event()
    started: list[subprocess.Popen[bytes]] = []

    def launch_then_cancel(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        process = real_popen(*args, **kwargs)
        started.append(process)
        interrupted.set()
        return process

    monkeypatch.setattr(subprocess, "Popen", launch_then_cancel)
    launcher = SubprocessLauncher()
    try:
        result = launcher.run_interruptible(
            (PYTHON, "-c", "import time; time.sleep(60)"),
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.5,
            interrupted=interrupted,
            poll_interval=0.005,
            term_grace=0.05,
            kill_reap=0.1,
        )
        assert result is None
        assert len(started) == 1 and started[0].poll() is not None
        assert not _group_exists(started[0].pid)
    finally:
        if started:
            try:
                os.killpg(started[0].pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class _ExitedFakeProcess:
    def __init__(self) -> None:
        self.pid = 44_444
        self.stdin = None
        self.stdout = None
        self.stderr = None

    def poll(self) -> int:
        return 0


class _GroupProbeFailureLauncher:
    def __init__(self) -> None:
        self.signals: list[tuple[int, signal.Signals]] = []
        self.waits: list[float] = []

    def signal_group(self, pgid: int, sig: signal.Signals) -> None:
        self.signals.append((pgid, sig))

    def process_group_exists(self, pgid: int) -> bool:
        raise PermissionError(f"cannot inspect group {pgid}")

    def wait(self, process: Any, timeout: float) -> int:
        self.waits.append(timeout)
        return 0


def test_process_group_probe_error_fails_closed_after_leader_exit() -> None:
    launcher = _GroupProbeFailureLauncher()
    child = _Child("source", cast(Any, _ExitedFakeProcess()), 44_444)
    errors: list[CleanupError] = []
    _stop_child(child, cast(Any, launcher), TIMING, errors)
    assert errors == [CleanupError("group-probe-source", "cannot inspect group 44444")]
    assert launcher.waits == [0]


class _WaitRecordingProcess:
    pid = 44_445

    def __init__(self) -> None:
        self.waits: list[float] = []

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        return 0


def test_one_shot_group_grace_does_not_add_a_separate_leader_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = _WaitRecordingProcess()
    probes = iter((True, False))
    monkeypatch.setattr(os, "killpg", lambda _pgid, _signal: None)
    _terminate_process_group(
        cast(Any, process),
        term_grace=0.01,
        kill_reap=0.02,
        poll_interval=0.001,
        pgid=process.pid,
        process_group_exists=lambda _pgid: next(probes),
    )
    assert process.waits == [0]


def _group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.mark.parametrize("ignore_term", [False, True])
def test_one_shot_quiesces_descendants_after_direct_parent_exit(
    tmp_path: Path, ignore_term: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = tmp_path / "child.pid"
    group_path = tmp_path / "group.id"
    ready_path = tmp_path / "child.ready"
    script = tmp_path / "fork_then_exit.py"
    script.write_text(
        "\n".join(
            [
                "import os, pathlib, signal, time",
                f"pid_path = pathlib.Path({str(pid_path)!r})",
                f"group_path = pathlib.Path({str(group_path)!r})",
                f"ready_path = pathlib.Path({str(ready_path)!r})",
                "pid = os.fork()",
                "if pid == 0:",
                f"    {'signal.signal(signal.SIGTERM, signal.SIG_IGN)' if ignore_term else 'pass'}",
                "    ready_path.write_text('ready', encoding='utf-8')",
                "    time.sleep(60)",
                "    os._exit(0)",
                "while not ready_path.exists():",
                "    time.sleep(0.001)",
                "pid_path.write_text(str(pid), encoding='utf-8')",
                "group_path.write_text(str(os.getpgrp()), encoding='utf-8')",
                "os._exit(0)",
            ]
        ),
        encoding="utf-8",
    )
    def descendant_group_exists(pgid: int) -> bool:
        if not pid_path.is_file():
            return True
        try:
            return os.getpgid(int(pid_path.read_text(encoding="utf-8"))) == pgid
        except ProcessLookupError:
            return False

    real_killpg = os.killpg
    signals: list[signal.Signals] = []

    def recording_killpg(pgid: int, sig: signal.Signals) -> None:
        if sig in {signal.SIGTERM, signal.SIGKILL}:
            signals.append(sig)
        real_killpg(pgid, sig)

    monkeypatch.setattr(os, "killpg", recording_killpg)
    launcher = SubprocessLauncher(process_group_exists=descendant_group_exists)
    group_id: int | None = None
    try:
        result = launcher._run_owned(
            (PYTHON, str(script)),
            env={"PATH": "/usr/bin:/bin"},
            timeout=0.5,
            interrupted=None,
            poll_interval=0.005,
            term_grace=0.08,
            kill_reap=0.2,
        )
        assert result == 0
        assert pid_path.is_file()
        assert ready_path.is_file()
        assert group_path.is_file()
        child_pid = int(pid_path.read_text(encoding="utf-8"))
        # The child PID differs from the group ID; the parent was the group leader.
        group_id = int(group_path.read_text(encoding="utf-8"))
        assert child_pid != group_id
        deadline = time.monotonic() + 1.0
        while descendant_group_exists(group_id) and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not descendant_group_exists(group_id)
        expected_signals = (
            [signal.SIGTERM, signal.SIGKILL]
            if ignore_term
            else [signal.SIGTERM]
        )
        assert signals == expected_signals
    finally:
        if group_id is not None:
            try:
                os.killpg(group_id, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, True), (3, False), (4, "error"), (9, "error"), (-15, "error")],
)
def test_system_probe_stock_status_is_fail_closed_tri_state(
    returncode: int, expected: bool | str
) -> None:
    probe = SystemProbe(launcher=_ProbeLauncher(returncode))
    if expected == "error":
        with pytest.raises(RuntimeError, match="stock service status"):
            probe.stock_service_active()
    else:
        assert probe.stock_service_active() is expected


class _InactiveMissingUdcProbe(MutableProbe):
    def read_text(self, path: Path) -> str:
        if not self.stock_active:
            raise FileNotFoundError("l4t UDC is absent while stock service is inactive")
        return super().read_text(path)


def test_inactive_free_restore_does_not_read_missing_prestart_l4t_udc(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    probe = _InactiveMissingUdcProbe()
    probe.stock_active = False
    probe.owners = frozenset()
    probe.l4t_udc = "udc-test\n"
    factory, _, launcher = _factory(tmp_path, plan, probe)
    result = factory.reconcile_stopped()
    assert result.success
    assert result.errors == ()
    assert any("stock-start" in " ".join(argv) for argv in launcher.commands)
    assert probe.stock_active
    assert probe.owners == frozenset({"l4t"})
    assert probe.l4t_udc.strip() == "udc-test"


@pytest.mark.parametrize("unknown", ["stock-active", "owners"])
def test_unknown_reconciliation_truth_never_starts_stock(
    tmp_path: Path, unknown: str
) -> None:
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.stock_active = False
    probe.owners = frozenset()
    probe.raise_on.add(unknown)
    factory, _, launcher = _factory(tmp_path, plan, probe)
    result = factory.reconcile_stopped()
    assert not result.success
    assert not any("stock-start" in " ".join(argv) for argv in launcher.commands)


def test_all_one_shot_boundaries_receive_exact_backend_timing(tmp_path: Path) -> None:
    timing = BackendTiming(0.2, 0.3, 0.41, 0.07, 0.09, 0.003)
    plan = _plan(tmp_path)
    probe = MutableProbe()
    probe.stock_active = False
    probe.owners = frozenset()
    launcher = RecordingLauncher(probe)
    factory, _, _ = _factory(tmp_path, plan, probe, launcher, timing=timing)
    cleanup = factory.reconcile_stopped()
    assert cleanup.success
    assert launcher.run_timings
    assert all(
        values == (
            timing.one_shot,
            timing.poll_interval,
            timing.term_grace,
            timing.kill_reap,
        )
        for values in launcher.run_timings
    )

    active_path = tmp_path / "active"
    active_plan = _plan(active_path, stock_stop_code=7)
    active_probe = MutableProbe()
    active_launcher = RecordingLauncher(active_probe)
    active_factory, _, _ = _factory(
        active_path, active_plan, active_probe, active_launcher, timing=timing
    )
    _, result, _ = _run(active_factory)
    assert result is not None and result.error == "stock-stop-failed"
    assert active_launcher.interrupt_timings[0] == (
        timing.one_shot,
        timing.poll_interval,
        timing.term_grace,
        timing.kill_reap,
    )
    assert all(
        values == (
            timing.one_shot,
            timing.poll_interval,
            timing.term_grace,
            timing.kill_reap,
        )
        for values in active_launcher.run_timings
    )


def test_default_system_probe_receives_exact_factory_timing(tmp_path: Path) -> None:
    timing = BackendTiming(0.2, 0.3, 0.43, 0.06, 0.08, 0.002)
    launcher = _ProbeLauncher(3)
    probe = SystemProbe(
        launcher=launcher,
        configfs_gadget_root=tmp_path,
        timing=timing,
    )
    assert probe.stock_service_active() is False
    assert launcher.calls == [
        (
            _STOCK_ACTIVE_ARGV_FOR_TEST,
            timing.one_shot,
            timing.poll_interval,
            timing.term_grace,
            timing.kill_reap,
        )
    ]


_STOCK_ACTIVE_ARGV_FOR_TEST = (
    "/usr/bin/systemctl",
    "is-active",
    "--quiet",
    "nv-l4t-usb-device-mode.service",
)


def test_missing_root_account_is_startup_failure_with_terminal_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    plan = LiveCommandPlan(
        xvfb=ProcessSpec(
            plan.xvfb.name,
            plan.xvfb.argv,
            plan.xvfb.env,
            run_as_user="missing_user",
        ),
        openbox=plan.openbox,
        chromium=plan.chromium,
        x11vnc=plan.x11vnc,
        websockify=plan.websockify,
        encoder=plan.encoder,
        gadget=plan.gadget,
        gadget_cleanup=plan.gadget_cleanup,
        stock_service_stop=plan.stock_service_stop,
        stock_service_start=plan.stock_service_start,
    )
    probe = MutableProbe()
    launcher = RecordingLauncher(probe, uid=0, user="root")
    monkeypatch.setattr(
        launcher,
        "passwd",
        lambda _user: (_ for _ in ()).throw(KeyError("missing account")),
    )
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    attempt, result, _ = _run(factory)
    assert result is not None
    assert result.error == "startup-failed: 'missing account'"
    assert attempt.stop() is result.cleanup
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "startup-failed"


def test_malformed_passwd_record_is_startup_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    plan = LiveCommandPlan(
        xvfb=ProcessSpec(plan.xvfb.name, plan.xvfb.argv, plan.xvfb.env, "alice"),
        openbox=plan.openbox,
        chromium=plan.chromium,
        x11vnc=plan.x11vnc,
        websockify=plan.websockify,
        encoder=plan.encoder,
        gadget=plan.gadget,
        gadget_cleanup=plan.gadget_cleanup,
        stock_service_stop=plan.stock_service_stop,
        stock_service_start=plan.stock_service_start,
    )
    probe = MutableProbe()
    launcher = RecordingLauncher(probe, uid=0, user="root")
    monkeypatch.setattr(launcher, "passwd", lambda _user: object())
    factory, _, _ = _factory(tmp_path, plan, probe, launcher)
    _, result, _ = _run(factory)
    assert result is not None
    assert result.error is not None and result.error.startswith("startup-failed:")
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "startup-failed"


def test_unexpected_internal_failure_finalizes_once_after_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))

    def explode(_publish: Any, _interrupted: Any) -> str:
        raise RuntimeError("injected lifecycle explosion")

    monkeypatch.setattr(attempt, "_active_lifecycle", explode)
    result = attempt.run(lambda _event: None, threading.Event())
    assert result is not None
    assert result.error == "internal-failure: injected lifecycle explosion"
    assert attempt.stop() is result.cleanup
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "internal-failure"


def test_internal_failure_after_streaming_preserves_metadata_and_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))

    def explode(_publish: Any, _interrupted: Any) -> str:
        attempt._product = "P"
        attempt._version = "V"
        attempt._streaming_at = attempt._monotonic() - 0.05
        raise RuntimeError("after streaming")

    monkeypatch.setattr(attempt, "_active_lifecycle", explode)
    result = attempt.run(lambda _event: None, threading.Event())
    assert result is not None
    assert result.error == "internal-failure: after streaming"
    assert result.product == "P" and result.version == "V"
    assert result.streaming_duration >= 0.05
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["product"] == "P" and document["version"] == "V"
    assert document["streaming_duration"] >= 0.05


def test_raising_publication_callback_is_contained_on_run_thread(tmp_path: Path) -> None:
    plan = _plan(tmp_path)
    factory, _, launcher = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))

    def reject(_event: AttemptEvent) -> None:
        raise RuntimeError("publisher rejected phase")

    result = attempt.run(reject, threading.Event())
    assert result is not None
    assert result.error == "internal-failure: publisher rejected phase"
    assert attempt.stop() is result.cleanup
    assert all(not worker.is_alive() for worker in attempt._workers)
    _assert_reaped(launcher)


@pytest.mark.parametrize("base_error", [KeyboardInterrupt(), SystemExit(7)])
def test_base_interrupt_reraises_only_after_cleanup_and_internal_interrupt_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, base_error: BaseException
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))

    def interrupt(_publish: Any, _interrupted: Any) -> str:
        attempt._product = "P"
        attempt._version = "V"
        attempt._streaming_at = attempt._monotonic() - 0.02
        raise base_error

    monkeypatch.setattr(attempt, "_active_lifecycle", interrupt)
    with pytest.raises(type(base_error)):
        attempt.run(lambda _event: None, threading.Event())
    cleanup = attempt.stop()
    evidence = next((tmp_path / "evidence").iterdir())
    document = json.loads((evidence / "RESULT.json").read_text(encoding="utf-8"))
    assert document["terminal_reason"] == "internal-interrupt"
    assert document["product"] == "P" and document["version"] == "V"
    assert document["streaming_duration"] >= 0.02
    assert document["cleanup"]["success"] is cleanup.success


def test_evidence_finalize_failure_settles_lifecycle_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    monkeypatch.setattr(
        attempt,
        "_active_lifecycle",
        lambda _publish, _interrupted: "forced-failure",
    )
    calls: list[str] = []

    def reject_finalize(self: Any, result: AttemptResult, *, terminal_reason: str) -> None:
        calls.append(terminal_reason)
        raise OSError("evidence disk full")

    monkeypatch.setattr(
        "hccast_wired.live.backend.RunEvidenceWriter.finalize", reject_finalize
    )
    result = attempt.run(lambda _event: None, threading.Event())
    assert result is not None
    assert result.error == "evidence-failed: evidence disk full"
    assert calls == ["forced-failure"]
    assert attempt.stop() is result.cleanup


class _ExitedProcess:
    pid = 999_998
    stdin = None
    stdout = None
    stderr = None

    def poll(self) -> int:
        return 7


@pytest.mark.parametrize(
    ("payload", "markers", "worker_error", "expected", "metadata"),
    [
        (b'{"product":"P","version":"V"}', True, False, "gadget-exited", ("P", "V")),
        (b'{"product":"P","version":}', True, False, "handshake-json", None),
        (b"x" * 65537, True, False, "handshake-json-too-large", None),
        (b'{"product":"P"', True, False, "handshake-json-incomplete", None),
        (b"", False, False, "gadget-stdout-eof", None),
        (b"", False, True, "stream-pipe", None),
        (b'{"product":"P","version":"V"}', False, False, "gadget-exited", ("P", "V")),
    ],
    ids=(
        "valid-dead",
        "malformed-json",
        "oversized-json",
        "incomplete-json",
        "clean-eof",
        "worker-error",
        "valid-without-markers",
    ),
)
def test_exited_gadget_waits_for_buffered_drain_before_stable_classification(
    tmp_path: Path,
    payload: bytes,
    markers: bool,
    worker_error: bool,
    expected: str,
    metadata: tuple[str, str] | None,
) -> None:
    plan = _plan(tmp_path)
    factory, _, _ = _factory(tmp_path, plan)
    attempt = cast(SubprocessAttempt, factory.create(LiveConfig(mode=DesiredMode.DESKTOP)))
    parser = _HandshakeParser(limit=65536)
    condition = threading.Condition()
    release_drain = threading.Event()
    drain_done = threading.Event()
    attempt._gadget_stdout_done = threading.Event()
    attempt._gadget_stderr_done = threading.Event()

    def delayed_drain() -> None:
        assert release_drain.wait(2)
        if markers:
            attempt._waiting_observed.set()
            attempt._setr_observed.set()
            parser.feed_stdout(_HANDSHAKE_PREFIX_FOR_TEST + payload)
        elif payload:
            parser.feed_stdout(_HANDSHAKE_PREFIX_FOR_TEST + payload)
        if worker_error:
            with attempt._worker_lock:
                attempt._worker_errors.append("gadget-stdout: injected drain failure")
        else:
            parser.stdout_eof()
        attempt._gadget_stdout_done.set()
        attempt._gadget_stderr_done.set()
        with condition:
            condition.notify_all()
        drain_done.set()

    feeder = threading.Thread(target=delayed_drain)
    feeder.start()
    outcome: list[object] = []
    waiter_done = threading.Event()

    def wait_for_handshake() -> None:
        outcome.append(
            attempt._wait_for_handshake(
                _Child("gadget-stream", cast(Any, _ExitedProcess()), 999_998),
                parser,
                condition,
                threading.Event(),
            )
        )
        waiter_done.set()

    waiter = threading.Thread(target=wait_for_handshake)
    waiter.start()
    returned_before_drain = waiter_done.wait(0.1)
    release_drain.set()
    waiter.join(2)
    feeder.join(2)
    assert not returned_before_drain
    assert drain_done.is_set() and not waiter.is_alive() and not feeder.is_alive()
    assert outcome == [expected]
    if metadata is not None:
        assert (attempt._product, attempt._version) == metadata


_HANDSHAKE_PREFIX_FOR_TEST = b"HCCAST handshake complete:"
