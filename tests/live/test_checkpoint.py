"""Contract tests for the bounded, single-purpose Jetson checkpoint runner."""

from __future__ import annotations

import threading
import time

import pytest

from hccast_wired.live.checkpoint import (
    _connect_event,
    _operation_policy,
    _parser,
    checkpoint_config,
    cleanup_to_mapping,
    demo_soak_config,
    kiosk_checkpoint_config,
    local_account_exists,
    main,
    run_demo_soak,
    run_once,
    validated_source_user,
)
from hccast_wired.live import checkpoint as checkpoint_module
from hccast_wired.live.model import DesiredMode, RuntimePhase
from hccast_wired.live.model import LiveConfig
from hccast_wired.live.supervisor import (
    AttemptClassification,
    AttemptEvent,
    AttemptResult,
    CleanupError,
    CleanupResult,
)
from hccast_wired.live.telemetry import TelemetryResult


# A neutral deployment account for these contract tests; nothing in the runner
# assumes any particular local account exists.
ACCOUNT = "alice"

SUCCESS = CleanupResult(
    attempted_actions=("gadget-cleanup", "stock-gadget-start"),
    errors=(),
    verified_postconditions=("stock-service-active", "owner-set:l4t"),
    success=True,
)


class FakeAttempt:
    def __init__(self, *, publish_streaming: bool) -> None:
        self.publish_streaming = publish_streaming
        self.run_calls = 0
        self.stop_calls = 0
        self.interrupted_seen = threading.Event()

    @property
    def started_monotonic(self) -> float:
        return 0.0

    def run(self, publish, interrupted):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        if self.publish_streaming:
            publish(AttemptEvent(RuntimePhase.WAITING_FOR_SCREEN))
            publish(AttemptEvent(RuntimePhase.HANDSHAKING, "HCT-AT01", "2505161526"))
            publish(AttemptEvent(RuntimePhase.STREAMING, "HCT-AT01", "2505161526"))
        interrupted.wait(1.0)
        self.interrupted_seen.set()
        return None

    def stop(self) -> CleanupResult:
        self.stop_calls += 1
        return SUCCESS


class FakeFactory:
    def __init__(self, attempt: FakeAttempt) -> None:
        self.attempt = attempt
        self.configs: list[LiveConfig] = []

    def create(self, config):  # type: ignore[no-untyped-def]
        self.configs.append(config)
        return self.attempt


class RaisingFactory:
    def create(self, config):  # type: ignore[no-untyped-def]
        raise RuntimeError("create failed")


class FakeDemoServer:
    def __init__(self, url: str) -> None:
        self.url = url
        self.start_calls = 0
        self.close_calls = 0
        self.running = False

    def start(self):  # type: ignore[no-untyped-def]
        self.start_calls += 1
        self.running = True
        return self

    def poll_failure(self) -> str | None:
        return None if self.running else "demo-server-exited"

    def close(self) -> None:
        self.close_calls += 1
        self.running = False


class FakeTelemetry:
    def __init__(self, *, success: bool, failure: str | None = None) -> None:
        self.success = success
        self.failure = failure
        self.close_calls = 0

    def poll_failure(self) -> str | None:
        return self.failure

    def close(self) -> TelemetryResult:
        self.close_calls += 1
        return TelemetryResult(
            log_path="/private/evidence/tegrastats.log",
            returncode=-15 if self.success else 7,
            success=self.success,
            error=None if self.success else "tegrastats exited 7",
        )


def test_checkpoint_config_freezes_the_approved_physical_settings() -> None:
    config = checkpoint_config(source_user=ACCOUNT)

    assert config.mode is DesiredMode.DESKTOP
    assert (config.width, config.height) == (640, 1136)
    assert config.fps == 10
    assert config.bitrate_kbps == 4000
    assert config.display_number == 99
    assert config.source_user == ACCOUNT
    assert config.novnc_enabled is False


def test_kiosk_checkpoint_config_targets_only_the_existing_local_open_webui() -> None:
    config = kiosk_checkpoint_config(source_user=ACCOUNT)

    assert config.mode is DesiredMode.KIOSK
    assert config.kiosk_url == "http://127.0.0.1:3000"
    assert (config.width, config.height) == (640, 1136)
    assert config.fps == 10
    assert config.bitrate_kbps == 4000
    assert config.source_user == ACCOUNT
    assert config.novnc_enabled is False


def test_demo_soak_config_freezes_approved_policy() -> None:
    config = demo_soak_config("http://127.0.0.1:8877/", source_user=ACCOUNT)

    assert config.mode is DesiredMode.KIOSK
    assert config.kiosk_url == "http://127.0.0.1:8877/"
    assert config.novnc_enabled is True
    assert (config.width, config.height) == (640, 1136)
    assert config.fps == 10
    assert config.bitrate_kbps == 4000
    assert config.display_number == 99
    assert config.source_user == ACCOUNT


def test_cleanup_mapping_preserves_every_action_error_and_postcondition() -> None:
    result = CleanupResult(
        attempted_actions=("cleanup",),
        errors=(CleanupError("stock-start", "exit 1"),),
        verified_postconditions=("hccast-root-absent",),
        success=False,
    )

    assert cleanup_to_mapping(result) == {
        "attempted_actions": ["cleanup"],
        "errors": [{"action": "stock-start", "message": "exit 1"}],
        "verified_postconditions": ["hccast-root-absent"],
        "success": False,
    }


def test_run_once_interrupts_twenty_second_policy_after_streaming_and_stops_once() -> None:
    attempt = FakeAttempt(publish_streaming=True)
    factory = FakeFactory(attempt)
    events: list[dict[str, object]] = []

    result = run_once(
        factory,
        checkpoint_config(source_user=ACCOUNT),
        streaming_seconds=0.02,
        total_deadline=1.0,
        shutdown_grace=0.5,
        emit=events.append,
    )

    assert attempt.interrupted_seen.wait(0.2)
    assert attempt.run_calls == 1
    assert attempt.stop_calls == 1
    assert factory.configs == [checkpoint_config(source_user=ACCOUNT)]
    assert result["terminal_reason"] == "streaming-window-complete"
    assert result["streaming_observed"] is True
    assert result["product"] == "HCT-AT01"
    assert result["version"] == "2505161526"
    assert result["cleanup"] == cleanup_to_mapping(SUCCESS)
    assert [event["phase"] for event in events] == [
        "waiting_for_screen",
        "handshaking",
        "streaming",
    ]


def test_run_once_total_deadline_interrupts_an_attempt_that_never_streams() -> None:
    attempt = FakeAttempt(publish_streaming=False)
    factory = FakeFactory(attempt)
    started = time.monotonic()

    result = run_once(
        factory,
        checkpoint_config(source_user=ACCOUNT),
        streaming_seconds=0.5,
        total_deadline=0.03,
        shutdown_grace=0.5,
        emit=lambda _event: None,
    )

    assert time.monotonic() - started < 0.5
    assert attempt.interrupted_seen.wait(0.2)
    assert attempt.stop_calls == 1
    assert result["terminal_reason"] == "total-deadline"
    assert result["streaming_observed"] is False
    assert result["cleanup"] == cleanup_to_mapping(SUCCESS)


def test_run_once_interrupts_immediately_when_health_fails() -> None:
    attempt = FakeAttempt(publish_streaming=True)

    result = run_once(
        FakeFactory(attempt),
        checkpoint_config(source_user=ACCOUNT),
        streaming_seconds=1.0,
        total_deadline=2.0,
        shutdown_grace=0.5,
        emit=lambda _event: None,
        health_failure=lambda: "telemetry-exited:7",
    )

    assert result["terminal_reason"] == "telemetry-exited:7"
    assert attempt.stop_calls == 1


class ReturningAttempt(FakeAttempt):
    def run(self, publish, interrupted):  # type: ignore[no-untyped-def]
        self.run_calls += 1
        return AttemptResult(
            classification=AttemptClassification.FAILURE,
            error="handshake-timeout",
            product=None,
            version=None,
            streaming_duration=0.0,
            cleanup=SUCCESS,
        )


def test_run_once_reports_early_attempt_failure_without_waiting_for_deadline() -> None:
    attempt = ReturningAttempt(publish_streaming=False)
    factory = FakeFactory(attempt)

    result = run_once(
        factory,
        checkpoint_config(source_user=ACCOUNT),
        streaming_seconds=1.0,
        total_deadline=1.0,
        shutdown_grace=0.5,
        emit=lambda _event: None,
    )

    assert attempt.stop_calls == 1
    assert result["terminal_reason"] == "attempt-returned"
    assert result["attempt_result"]["classification"] == "failure"  # type: ignore[index]
    assert result["attempt_result"]["error"] == "handshake-timeout"  # type: ignore[index]


def test_demo_soak_uses_short_durations_and_closes_auxiliary_work() -> None:
    attempt = FakeAttempt(publish_streaming=True)
    factory = FakeFactory(attempt)
    server = FakeDemoServer("http://127.0.0.1:8877/")
    telemetry = FakeTelemetry(success=True)

    result = run_demo_soak(
        factory,
        server=server,
        telemetry=telemetry,
        source_user=ACCOUNT,
        streaming_seconds=0.02,
        total_deadline=1.0,
        shutdown_grace=0.5,
        emit=lambda _event: None,
    )

    assert result["streaming_observed"] is True
    assert result["telemetry"]["success"] is True  # type: ignore[index]
    assert server.start_calls == 1
    assert server.close_calls == 1
    assert telemetry.close_calls == 1


def test_demo_soak_closes_server_and_telemetry_when_attempt_factory_raises() -> None:
    server = FakeDemoServer("http://127.0.0.1:8877/")
    telemetry = FakeTelemetry(success=True)

    with pytest.raises(RuntimeError, match="create failed"):
        run_demo_soak(
            RaisingFactory(),
            server=server,
            telemetry=telemetry,
            source_user=ACCOUNT,
            streaming_seconds=0.02,
            total_deadline=1.0,
            shutdown_grace=0.5,
            emit=lambda _event: None,
        )

    assert server.close_calls == 1
    assert telemetry.close_calls == 1


def test_demo_soak_cli_policy_is_exact_and_preserves_existing_policy() -> None:
    args = _parser().parse_args(
        [
            "run-demo-soak",
            "--expected-udc",
            "3550000.usb",
            "--evidence-root",
            "/private/evidence",
            "--hccast-executable",
            "/opt/hccast-wired",
            "--source-user",
            ACCOUNT,
        ]
    )

    assert args.operation == "run-demo-soak"
    assert args.source_user == ACCOUNT
    assert _operation_policy("run-demo-soak") == (1200.0, 1380.0, 20.0)
    assert _operation_policy("run-kiosk-once") == (20.0, 180.0, 15.0)
    assert _connect_event("run-demo-soak") == {
        "event": "connect-data-now",
        "message": "CONNECT DATA NOW",
        "deadline_seconds": 1380,
        "streaming_window_seconds": 1200,
    }


def test_checkpoint_configs_require_an_explicit_deployment_account() -> None:
    with pytest.raises(TypeError):
        checkpoint_config()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        kiosk_checkpoint_config()  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        demo_soak_config("http://127.0.0.1:8877/")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="source_user"):
        checkpoint_config(source_user="Not A Posix Name")

    assert checkpoint_config(source_user="pi").source_user == "pi"
    assert kiosk_checkpoint_config(source_user="pi").source_user == "pi"
    assert demo_soak_config("http://127.0.0.1:8877/", source_user="pi").source_user == "pi"


def test_cli_requires_the_deployment_account_argument() -> None:
    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "run-once",
                "--expected-udc",
                "3550000.usb",
                "--evidence-root",
                "/private/evidence",
                "--hccast-executable",
                "/opt/hccast-wired",
            ]
        )


@pytest.mark.parametrize("name", ["", "Two Words", "AUser", "user!", "../root"])
def test_validated_source_user_rejects_malformed_names_without_consulting_the_host(
    name: str,
) -> None:
    consulted: list[str] = []

    with pytest.raises(SystemExit, match="--source-user is invalid"):
        validated_source_user(name, account_exists=lambda value: consulted.append(value) or True)

    assert consulted == []


def test_validated_source_user_refuses_an_account_missing_from_the_host() -> None:
    with pytest.raises(SystemExit, match="names no account on this host"):
        validated_source_user("stranger", account_exists=lambda _name: False)

    assert validated_source_user("stranger", account_exists=lambda _name: True) == "stranger"


def test_local_account_lookup_uses_the_host_password_database() -> None:
    assert local_account_exists("root") is True
    assert local_account_exists("no-such-account-for-hccast-tests") is False


def _cli_arguments(operation: str, account: str) -> list[str]:
    return [
        operation,
        "--expected-udc",
        "3550000.usb",
        "--evidence-root",
        "/private/evidence",
        "--hccast-executable",
        "/opt/hccast-wired",
        "--source-user",
        account,
    ]


def test_main_refuses_an_unknown_account_before_building_any_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_factory(_args: object) -> object:
        raise AssertionError("no factory may be built for a refused account")

    monkeypatch.setattr(checkpoint_module, "_factory", forbidden_factory)

    with pytest.raises(SystemExit, match="names no account on this host"):
        main(_cli_arguments("reconcile-only", "stranger"), account_exists=lambda _name: False)
    with pytest.raises(SystemExit, match="--source-user is invalid"):
        main(_cli_arguments("run-once", "Bad Name"), account_exists=lambda _name: True)


def test_main_passes_the_validated_account_to_the_checkpoint_configuration(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    attempt = FakeAttempt(publish_streaming=True)
    factory = FakeFactory(attempt)
    monkeypatch.setattr(checkpoint_module, "_factory", lambda _args: factory)
    monkeypatch.setattr(checkpoint_module, "_operation_policy", lambda _operation: (0.02, 1.0, 0.5))
    monkeypatch.setattr(checkpoint_module.signal, "signal", lambda _signum, _handler: None)

    code = main(_cli_arguments("run-once", "pi"), account_exists=lambda name: name == "pi")

    assert code == 0
    assert factory.configs == [checkpoint_config(source_user="pi")]
    assert '"connect-data-now"' in capsys.readouterr().out
