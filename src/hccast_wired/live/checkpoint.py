"""Bounded, single-attempt Jetson hardware checkpoint runner.

This module exposes only bounded operations approved by the post-Task-4
checkpoint contracts.  It is not a persistent service or a general
live-controller CLI.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
import json
from pathlib import Path
import signal
import threading
import time
from typing import Protocol

from hccast_wired.live.backend import ReconciliationTargets, SubprocessAttemptFactory
from hccast_wired.live.commands import JetsonCommandBuilder
from hccast_wired.live.demo import DemoPageServer
from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase
from hccast_wired.live.supervisor import (
    AttemptEvent,
    AttemptResult,
    CleanupResult,
    LiveAttempt,
)
from hccast_wired.live.telemetry import (
    SubprocessTelemetryLauncher,
    TelemetryRecorder,
    TelemetryResult,
)


class _AttemptCreator(Protocol):
    def create(self, config: LiveConfig) -> LiveAttempt: ...


class _DemoServer(Protocol):
    @property
    def url(self) -> str: ...

    def start(self) -> _DemoServer: ...

    def poll_failure(self) -> str | None: ...

    def close(self) -> None: ...


class _Telemetry(Protocol):
    def poll_failure(self) -> str | None: ...

    def close(self) -> TelemetryResult: ...


def checkpoint_config() -> LiveConfig:
    """Return the exact configuration frozen in the physical checkpoint plan."""

    return LiveConfig(
        mode=DesiredMode.DESKTOP,
        width=640,
        height=1136,
        fps=10,
        bitrate_kbps=4000,
        display_number=99,
        source_user="ama",
        novnc_enabled=False,
    )


def kiosk_checkpoint_config() -> LiveConfig:
    """Return the exact local-Open-WebUI kiosk checkpoint configuration."""

    return checkpoint_config().with_updates(
        mode=DesiredMode.KIOSK,
        kiosk_url="http://127.0.0.1:3000",
    )


def demo_soak_config(url: str) -> LiveConfig:
    """Return the approved 20-minute local demo checkpoint configuration."""

    return checkpoint_config().with_updates(
        mode=DesiredMode.KIOSK,
        kiosk_url=url,
        novnc_enabled=True,
    )


def _emit_json(event: dict[str, object]) -> None:
    print(json.dumps(event, sort_keys=True), flush=True)


def cleanup_to_mapping(result: CleanupResult) -> dict[str, object]:
    """Serialize complete checked-cleanup evidence without dropping failures."""

    return {
        "attempted_actions": list(result.attempted_actions),
        "errors": [
            {"action": error.action, "message": error.message} for error in result.errors
        ],
        "verified_postconditions": list(result.verified_postconditions),
        "success": result.success,
    }


def _attempt_result_to_mapping(result: AttemptResult | None) -> dict[str, object] | None:
    if result is None:
        return None
    return {
        "classification": result.classification.value,
        "error": result.error,
        "product": result.product,
        "version": result.version,
        "streaming_duration": result.streaming_duration,
        "cleanup": cleanup_to_mapping(result.cleanup),
    }


def run_once(
    factory: _AttemptCreator,
    config: LiveConfig,
    *,
    streaming_seconds: float = 20.0,
    total_deadline: float = 180.0,
    shutdown_grace: float = 15.0,
    emit: Callable[[dict[str, object]], None] = lambda event: print(
        json.dumps(event, sort_keys=True), flush=True
    ),
    external_interrupt: threading.Event | None = None,
    health_failure: Callable[[], str | None] | None = None,
) -> dict[str, object]:
    """Run exactly one attempt, interrupting normally after the bounded window."""

    if min(streaming_seconds, total_deadline, shutdown_grace) <= 0:
        raise ValueError("checkpoint durations must be positive")

    attempt = factory.create(config)
    interrupted = threading.Event()
    streaming = threading.Event()
    finished = threading.Event()
    result_holder: list[AttemptResult | None] = []
    error_holder: list[BaseException] = []
    product: str | None = None
    version: str | None = None
    streaming_started: float | None = None

    def publish(event: AttemptEvent) -> None:
        nonlocal product, version, streaming_started
        product = event.product or product
        version = event.version or version
        emit(
            {
                "event": "attempt-phase",
                "phase": event.phase.value,
                "product": event.product,
                "version": event.version,
            }
        )
        if event.phase is RuntimePhase.STREAMING and streaming_started is None:
            streaming_started = time.monotonic()
            streaming.set()

    def worker() -> None:
        try:
            result_holder.append(attempt.run(publish, interrupted))
        except BaseException as error:
            error_holder.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=worker, name="hccast-checkpoint-attempt", daemon=True)
    started = time.monotonic()
    thread.start()
    terminal_reason = "attempt-returned"

    while not finished.wait(0.05):
        now = time.monotonic()
        if external_interrupt is not None and external_interrupt.is_set():
            terminal_reason = "external-interrupt"
            interrupted.set()
            break
        if health_failure is not None:
            failure = health_failure()
            if failure is not None:
                terminal_reason = failure
                interrupted.set()
                break
        if streaming.is_set() and streaming_started is not None:
            if now - streaming_started >= streaming_seconds:
                terminal_reason = "streaming-window-complete"
                interrupted.set()
                break
        if now - started >= total_deadline:
            terminal_reason = "total-deadline"
            interrupted.set()
            break

    if interrupted.is_set():
        thread.join(shutdown_grace)
    cleanup = attempt.stop()
    if thread.is_alive():
        thread.join(shutdown_grace)
    if thread.is_alive():
        terminal_reason = "worker-stuck"

    attempt_result = result_holder[0] if result_holder else None
    return {
        "terminal_reason": terminal_reason,
        "streaming_observed": streaming.is_set(),
        "product": product,
        "version": version,
        "worker_alive": thread.is_alive(),
        "worker_error": (
            f"{error_holder[0].__class__.__name__}: {error_holder[0]}"
            if error_holder
            else None
        ),
        "attempt_result": _attempt_result_to_mapping(attempt_result),
        "cleanup": cleanup_to_mapping(cleanup),
    }


def run_demo_soak(
    factory: _AttemptCreator,
    *,
    server: _DemoServer,
    telemetry: _Telemetry,
    streaming_seconds: float = 1200.0,
    total_deadline: float = 1380.0,
    shutdown_grace: float = 20.0,
    emit: Callable[[dict[str, object]], None] = _emit_json,
    external_interrupt: threading.Event | None = None,
) -> dict[str, object]:
    """Compose one bounded attempt with owned source and telemetry helpers."""

    telemetry_result: TelemetryResult
    try:
        server.start()
        result = run_once(
            factory,
            demo_soak_config(server.url),
            streaming_seconds=streaming_seconds,
            total_deadline=total_deadline,
            shutdown_grace=shutdown_grace,
            emit=emit,
            external_interrupt=external_interrupt,
            health_failure=lambda: server.poll_failure() or telemetry.poll_failure(),
        )
    finally:
        try:
            telemetry_result = telemetry.close()
        finally:
            server.close()

    result["telemetry"] = telemetry_result.to_mapping()
    terminal_reason = str(result["terminal_reason"])
    if not telemetry_result.success and not terminal_reason.startswith("telemetry-exited:"):
        result["terminal_reason"] = "telemetry-failed"
    return result


def _factory(args: argparse.Namespace) -> SubprocessAttemptFactory:
    builder = JetsonCommandBuilder(hccast_executable=args.hccast_executable)
    targets = ReconciliationTargets(expected_udc=args.expected_udc)
    return SubprocessAttemptFactory(
        build_plan=builder.build,
        reconciliation=builder.build_reconciliation(),
        targets=targets,
        evidence_root=Path(args.evidence_root),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "reconcile-only",
            "run-once",
            "run-kiosk-once",
            "run-demo-soak",
            "verify-stopped",
        ),
    )
    parser.add_argument("--expected-udc", required=True)
    parser.add_argument("--evidence-root", required=True)
    parser.add_argument(
        "--hccast-executable",
        required=True,
        help="absolute path to the installed hccast-wired executable",
    )
    return parser


def _operation_policy(operation: str) -> tuple[float, float, float]:
    if operation == "run-demo-soak":
        return 1200.0, 1380.0, 20.0
    if operation in {"run-once", "run-kiosk-once"}:
        return 20.0, 180.0, 15.0
    raise ValueError(f"operation has no active policy: {operation}")


def _connect_event(operation: str) -> dict[str, object]:
    streaming_seconds, total_deadline, _shutdown_grace = _operation_policy(operation)
    return {
        "event": "connect-data-now",
        "message": "CONNECT DATA NOW",
        "deadline_seconds": int(total_deadline),
        "streaming_window_seconds": int(streaming_seconds),
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not Path(args.evidence_root).is_absolute():
        raise SystemExit("--evidence-root must be absolute")
    if not Path(args.hccast_executable).is_absolute():
        raise SystemExit("--hccast-executable must be absolute")

    factory = _factory(args)
    if args.operation in {"reconcile-only", "verify-stopped"}:
        cleanup = factory.reconcile_stopped()
        print(json.dumps({"operation": args.operation, "cleanup": cleanup_to_mapping(cleanup)}))
        return 0 if cleanup.success else 1

    external_interrupt = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        external_interrupt.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    streaming_seconds, total_deadline, shutdown_grace = _operation_policy(args.operation)
    print(json.dumps(_connect_event(args.operation)), flush=True)
    if args.operation == "run-demo-soak":
        server: DemoPageServer | None = None
        telemetry: TelemetryRecorder | None = None
        try:
            server = DemoPageServer()
            telemetry = TelemetryRecorder.start(
                Path(args.evidence_root),
                launcher=SubprocessTelemetryLauncher(),
                token=f"soak-{time.time_ns()}",
            )
            result = run_demo_soak(
                factory,
                server=server,
                telemetry=telemetry,
                streaming_seconds=streaming_seconds,
                total_deadline=total_deadline,
                shutdown_grace=shutdown_grace,
                external_interrupt=external_interrupt,
            )
        except Exception as caught:
            if telemetry is not None:
                telemetry.close()
            if server is not None:
                server.close()
            cleanup = factory.reconcile_stopped()
            print(
                json.dumps(
                    {
                        "operation": args.operation,
                        "startup_error": f"{caught.__class__.__name__}: {caught}",
                        "cleanup": cleanup_to_mapping(cleanup),
                    },
                    sort_keys=True,
                )
            )
            return 1
    else:
        config = (
            kiosk_checkpoint_config()
            if args.operation == "run-kiosk-once"
            else checkpoint_config()
        )
        result = run_once(
            factory,
            config,
            streaming_seconds=streaming_seconds,
            total_deadline=total_deadline,
            shutdown_grace=shutdown_grace,
            external_interrupt=external_interrupt,
        )
    print(json.dumps({"operation": args.operation, "result": result}, sort_keys=True))
    final_cleanup = result["cleanup"]
    assert isinstance(final_cleanup, dict)
    telemetry_mapping = result.get("telemetry")
    telemetry_ok = (
        telemetry_mapping is None
        or isinstance(telemetry_mapping, dict)
        and telemetry_mapping.get("success") is True
    )
    return (
        0
        if final_cleanup.get("success") is True
        and result["worker_alive"] is False
        and telemetry_ok
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
