from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Callable, cast

import pytest

from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase
from hccast_wired.live.supervisor import (
    AttemptClassification,
    AttemptEvent,
    AttemptResult,
    CleanupError,
    CleanupResult,
    LiveSupervisor,
)


def _cleanup_ok() -> CleanupResult:
    return CleanupResult(
        attempted_actions=("remove-hccast", "restore-stock-gadget"),
        errors=(),
        verified_postconditions=("hccast-absent", "stock-gadget-owns-udc"),
        success=True,
    )


def _cleanup_failure(action: str = "restore-stock-gadget") -> CleanupResult:
    return CleanupResult(
        attempted_actions=("remove-hccast", action),
        errors=(CleanupError(action=action, message="verification failed"),),
        verified_postconditions=("hccast-absent",),
        success=False,
    )


def _failure_result(
    *,
    duration: float = 0.0,
    cleanup: CleanupResult | None = None,
    product: str | None = None,
    version: str | None = None,
) -> AttemptResult:
    return AttemptResult(
        classification=AttemptClassification.FAILURE,
        error="transport ended",
        product=product,
        version=version,
        streaming_duration=duration,
        cleanup=cleanup or _cleanup_ok(),
    )


class FakeClock:
    def __init__(self) -> None:
        self._seconds = 0.0
        self._epoch = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        with self._lock:
            return self._seconds

    def utc_now(self) -> datetime:
        with self._lock:
            return self._epoch + timedelta(seconds=self._seconds)

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._seconds += seconds


class FakeWaitStrategy:
    """A retry wait controlled by tests and woken by the supervisor condition."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.delays: list[float] = []
        self._condition: threading.Condition | None = None
        self._permits = 0
        self._condition_ready = threading.Event()

    def wait(
        self,
        condition: threading.Condition,
        interrupted: Callable[[], bool],
        timeout: float,
    ) -> bool:
        with condition:
            self._condition = condition
            self.delays.append(timeout)
            self._condition_ready.set()
            condition.notify_all()
            while self._permits == 0 and not interrupted():
                condition.wait()
            if interrupted():
                return True
            self._permits -= 1
        self.clock.advance(timeout)
        return False

    def release_one(self) -> None:
        assert self._condition_ready.wait(1.0)
        condition = self._condition
        assert condition is not None
        with condition:
            self._permits += 1
            condition.notify_all()


class TerminalWaitGate:
    """Hold a retry after its status publication for a deterministic observation."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.allow_return = threading.Event()
        self._condition: threading.Condition | None = None

    def wait(
        self,
        condition: threading.Condition,
        interrupted: Callable[[], bool],
        timeout: float,
    ) -> bool:
        del interrupted, timeout
        with condition:
            self._condition = condition
            self.entered.set()
            condition.notify_all()
            condition.wait_for(self.allow_return.is_set)
        return True

    def release(self) -> None:
        condition = self._condition
        if condition is not None:
            with condition:
                self.allow_return.set()
                condition.notify_all()
        else:
            self.allow_return.set()


class FakeStore:
    def __init__(self, config: LiveConfig | None = None) -> None:
        self.config = config or LiveConfig()
        self.saved: list[LiveConfig] = []

    def load(self) -> LiveConfig:
        return self.config

    def save(self, config: LiveConfig) -> None:
        self.config = config
        self.saved.append(config)


class GatedFailureResult:
    """Pause after result acceptance but before retry-status publication."""

    classification = AttemptClassification.FAILURE
    error = "transport ended"
    product: str | None = None
    version: str | None = None

    def __init__(self, cleanup: CleanupResult) -> None:
        self.cleanup = cleanup
        self.ready_for_generation_change = threading.Event()
        self.allow_terminal_publication = threading.Event()

    @property
    def streaming_duration(self) -> float:
        self.ready_for_generation_change.set()
        assert self.allow_terminal_publication.wait(1.0)
        return 0.0


@dataclass
class FakeAttemptScript:
    events: tuple[AttemptEvent, ...] = ()
    result: AttemptResult | GatedFailureResult | None = None
    run_error: BaseException | None = None
    block_until_cancelled: bool = False
    gate_events: bool = False
    before_return: threading.Event | None = None
    allow_return: threading.Event | None = None
    stop_cleanup: CleanupResult = field(default_factory=_cleanup_ok)


class FakeAttempt:
    def __init__(
        self,
        *,
        factory: FakeAttemptFactory,
        identifier: int,
        script: FakeAttemptScript,
        started_monotonic: float,
    ) -> None:
        self.factory = factory
        self.identifier = identifier
        self.script = script
        self.started_monotonic = started_monotonic
        self.started = threading.Event()
        self.stop_calls = 0
        self._phase_condition = threading.Condition()
        self._published_events = 0
        self._event_permits = 0

    def run(
        self,
        publish: Callable[[AttemptEvent], None],
        interrupted: threading.Event,
    ) -> AttemptResult | None:
        with self.factory._activity_lock:
            self.factory.active_attempts += 1
            self.factory.max_active_attempts = max(
                self.factory.max_active_attempts, self.factory.active_attempts
            )
        self.started.set()
        try:
            for event in self.script.events:
                publish(event)
                with self._phase_condition:
                    self._published_events += 1
                    self._phase_condition.notify_all()
                    while self.script.gate_events and self._event_permits == 0:
                        self._phase_condition.wait()
                    if self.script.gate_events:
                        self._event_permits -= 1
                if interrupted.is_set():
                    return None
            if self.script.block_until_cancelled:
                interrupted.wait()
                return None
            if self.script.before_return is not None:
                self.script.before_return.set()
            if self.script.allow_return is not None:
                self.script.allow_return.wait()
            if self.script.run_error is not None:
                raise self.script.run_error
            return cast(AttemptResult, self.script.result or _failure_result())
        finally:
            with self.factory._activity_lock:
                self.factory.active_attempts -= 1

    def stop(self) -> CleanupResult:
        self.stop_calls += 1
        self.factory.lifecycle.append(("stop", self.identifier))
        return self.script.stop_cleanup

    def wait_for_published_events(self, count: int) -> None:
        _eventually(lambda: self._published_events >= count)

    def allow_next_event(self) -> None:
        with self._phase_condition:
            self._event_permits += 1
            self._phase_condition.notify_all()


class FakeAttemptFactory:
    def __init__(
        self,
        clock: FakeClock,
        scripts: list[FakeAttemptScript] | None = None,
        reconciliations: list[CleanupResult] | None = None,
    ) -> None:
        self.clock = clock
        self.scripts = list(scripts or [])
        self.reconciliations = list(reconciliations or [])
        self.created: list[FakeAttempt] = []
        self.created_configs: list[LiveConfig] = []
        self.lifecycle: list[tuple[str, object]] = []
        self.reconcile_calls = 0
        self.active_attempts = 0
        self.max_active_attempts = 0
        self._activity_lock = threading.Lock()

    def create(self, config: LiveConfig) -> FakeAttempt:
        script = self.scripts.pop(0) if self.scripts else FakeAttemptScript(
            block_until_cancelled=True
        )
        identifier = len(self.created) + 1
        attempt = FakeAttempt(
            factory=self,
            identifier=identifier,
            script=script,
            started_monotonic=self.clock.monotonic(),
        )
        self.created.append(attempt)
        self.created_configs.append(config)
        self.lifecycle.append(("create", config.mode))
        return attempt

    def reconcile_stopped(self) -> CleanupResult:
        self.reconcile_calls += 1
        self.lifecycle.append(("reconcile", self.reconcile_calls))
        if self.reconciliations:
            return self.reconciliations.pop(0)
        return _cleanup_ok()


class GatedReconcileFactory(FakeAttemptFactory):
    """Pause the second reconciliation after a stale attempt has returned."""

    def __init__(self, clock: FakeClock, scripts: list[FakeAttemptScript]) -> None:
        super().__init__(clock, scripts=scripts)
        self.second_reconcile_entered = threading.Event()
        self.allow_second_reconcile = threading.Event()

    def reconcile_stopped(self) -> CleanupResult:
        result = super().reconcile_stopped()
        if self.reconcile_calls == 2:
            self.second_reconcile_entered.set()
            self.allow_second_reconcile.wait()
        return result


class ExplodingWaitStrategy:
    """Force an exception to escape the worker's ordinary attempt handling."""

    def __init__(self) -> None:
        self.entered = threading.Event()

    def wait(
        self,
        condition: threading.Condition,
        interrupted: Callable[[], bool],
        timeout: float,
    ) -> bool:
        del condition, interrupted, timeout
        self.entered.set()
        raise RuntimeError("worker wait exploded")


class ExitGatedSupervisor(LiveSupervisor):
    """Keep a failed worker alive after publishing its terminal cleanup."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.closed_result_published = threading.Event()
        self.allow_worker_exit = threading.Event()

    def _publish_closed(self, result: CleanupResult) -> None:
        super()._publish_closed(result)
        self.closed_result_published.set()
        self.allow_worker_exit.wait()


def _eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    scheduler_yield = threading.Event()
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition was not reached before the test deadline")
        scheduler_yield.wait(0.001)


def _supervisor(
    *,
    config: LiveConfig | None = None,
    scripts: list[FakeAttemptScript] | None = None,
    reconciliations: list[CleanupResult] | None = None,
) -> tuple[LiveSupervisor, FakeStore, FakeAttemptFactory, FakeClock, FakeWaitStrategy]:
    clock = FakeClock()
    store = FakeStore(config)
    factory = FakeAttemptFactory(clock, scripts=scripts, reconciliations=reconciliations)
    wait_strategy = FakeWaitStrategy(clock)
    supervisor = LiveSupervisor(
        store=store,
        attempt_factory=factory,
        clock=clock,
        wait_strategy=wait_strategy,
    )
    return supervisor, store, factory, clock, wait_strategy


def test_fresh_store_remains_stopped_until_selection() -> None:
    supervisor, store, factory, _, _ = _supervisor()

    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    assert store.config.mode is DesiredMode.STOPPED
    assert factory.created == []
    assert supervisor.close().success


def test_select_desktop_persists_and_starts_exactly_one_attempt() -> None:
    supervisor, store, factory, _, _ = _supervisor()
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    returned = supervisor.select_mode(DesiredMode.DESKTOP)
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    assert returned.desired_mode is DesiredMode.DESKTOP
    assert store.config.mode is DesiredMode.DESKTOP
    assert len(factory.created) == 1
    assert factory.max_active_attempts == 1
    supervisor.close()


def test_phase_events_progress_to_streaming() -> None:
    events = (
        AttemptEvent(RuntimePhase.WAITING_FOR_SCREEN),
        AttemptEvent(RuntimePhase.HANDSHAKING),
        AttemptEvent(RuntimePhase.STREAMING, product="HCT-AT01", version="2505161526"),
    )
    script = FakeAttemptScript(events=events, result=_failure_result(), gate_events=True)
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP), scripts=[script]
    )

    supervisor.start()
    _eventually(lambda: len(factory.created) == 1)
    attempt = factory.created[0]
    attempt.wait_for_published_events(1)
    assert supervisor.snapshot().phase is RuntimePhase.WAITING_FOR_SCREEN

    attempt.allow_next_event()
    attempt.wait_for_published_events(2)
    assert supervisor.snapshot().phase is RuntimePhase.HANDSHAKING

    attempt.allow_next_event()
    attempt.wait_for_published_events(3)
    status = supervisor.snapshot()
    assert status.phase is RuntimePhase.STREAMING
    assert (status.product, status.version) == ("HCT-AT01", "2505161526")

    attempt.allow_next_event()
    supervisor.close()


def test_failure_retries_at_2_4_8_16_30_seconds() -> None:
    scripts = [FakeAttemptScript(result=_failure_result()) for _ in range(5)]
    supervisor, _, factory, _, wait_strategy = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP), scripts=scripts
    )
    supervisor.start()

    expected = [2.0, 4.0, 8.0, 16.0, 30.0]
    for index, delay in enumerate(expected, start=1):
        _eventually(lambda: len(wait_strategy.delays) >= index)
        assert wait_strategy.delays[index - 1] == delay
        if index < len(expected):
            wait_strategy.release_one()
            _eventually(lambda: len(factory.created) >= index + 1)

    assert wait_strategy.delays == expected
    supervisor.close()


def test_sixty_second_stream_resets_retry_delay() -> None:
    scripts = [
        FakeAttemptScript(result=_failure_result()),
        FakeAttemptScript(result=_failure_result()),
        FakeAttemptScript(result=_failure_result(duration=60.0)),
    ]
    supervisor, _, _, _, wait_strategy = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP), scripts=scripts
    )
    supervisor.start()

    _eventually(lambda: len(wait_strategy.delays) >= 1)
    wait_strategy.release_one()
    _eventually(lambda: len(wait_strategy.delays) >= 2)
    wait_strategy.release_one()
    _eventually(lambda: len(wait_strategy.delays) >= 3)

    assert wait_strategy.delays[:3] == [2.0, 4.0, 2.0]
    supervisor.close()


def test_mode_change_stops_old_attempt_before_new_attempt() -> None:
    scripts = [
        FakeAttemptScript(block_until_cancelled=True),
        FakeAttemptScript(block_until_cancelled=True),
    ]
    supervisor, _, factory, _, _ = _supervisor(scripts=scripts)
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    supervisor.select_mode(DesiredMode.DESKTOP)
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())
    old_attempt = factory.created[0]
    supervisor.select_mode(DesiredMode.KIOSK)
    _eventually(lambda: len(factory.created) == 2 and factory.created[1].started.is_set())

    assert old_attempt.stop_calls == 1
    stop_index = factory.lifecycle.index(("stop", 1))
    kiosk_index = factory.lifecycle.index(("create", DesiredMode.KIOSK))
    assert stop_index < kiosk_index
    assert factory.max_active_attempts == 1
    supervisor.close()


def test_stop_cancels_pending_retry_and_restores_stock_gadget() -> None:
    supervisor, _, factory, _, wait_strategy = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        scripts=[FakeAttemptScript(result=_failure_result())],
    )
    supervisor.start()
    _eventually(lambda: len(wait_strategy.delays) == 1)

    supervisor.select_mode(DesiredMode.STOPPED)
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    assert len(factory.created) == 1
    assert factory.reconcile_calls >= 3
    assert factory.lifecycle[-1][0] == "reconcile"
    supervisor.close()


def test_restart_does_not_change_persisted_mode() -> None:
    scripts = [
        FakeAttemptScript(block_until_cancelled=True),
        FakeAttemptScript(block_until_cancelled=True),
    ]
    supervisor, store, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP), scripts=scripts
    )
    supervisor.start()
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    returned = supervisor.restart()
    _eventually(lambda: len(factory.created) == 2 and factory.created[1].started.is_set())

    assert returned.desired_mode is DesiredMode.DESKTOP
    assert store.config.mode is DesiredMode.DESKTOP
    assert store.saved == []
    assert factory.created[0].stop_calls == 1
    supervisor.close()


def test_update_config_restarts_active_mode_only_for_an_effective_change() -> None:
    scripts = [
        FakeAttemptScript(block_until_cancelled=True),
        FakeAttemptScript(block_until_cancelled=True),
    ]
    supervisor, store, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP), scripts=scripts
    )
    supervisor.start()
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    unchanged = supervisor.update_config(fps=10)
    assert unchanged.desired_mode is DesiredMode.DESKTOP
    assert store.saved == []
    assert len(factory.created) == 1

    supervisor.update_config(fps=12)
    _eventually(lambda: len(factory.created) == 2 and factory.created[1].started.is_set())
    assert store.config.fps == 12
    assert factory.created[0].stop_calls == 1
    supervisor.close()


def test_close_is_idempotent_and_restores_stock_gadget() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        scripts=[FakeAttemptScript(block_until_cancelled=True)],
    )
    supervisor.start()
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    first = supervisor.close()
    reconcile_calls = factory.reconcile_calls
    second = supervisor.close()

    assert first == second
    assert first.success
    assert "restore-stock-gadget" in first.attempted_actions
    assert factory.created[0].stop_calls == 1
    assert factory.reconcile_calls == reconcile_calls


def test_cleanup_failure_reports_error_instead_of_success() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        reconciliations=[_cleanup_ok(), _cleanup_failure()]
    )
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    result = supervisor.close()

    assert not result.success
    assert result.errors == (
        CleanupError(action="restore-stock-gadget", message="verification failed"),
    )
    assert supervisor.snapshot().phase is RuntimePhase.ERROR
    assert "restore-stock-gadget: verification failed" in (supervisor.snapshot().last_error or "")
    assert factory.reconcile_calls == 2


def test_startup_stopped_reconciles_stale_gadget_before_reporting_stopped() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        reconciliations=[
            CleanupResult(
                attempted_actions=("remove-stale-hccast", "restore-stock-gadget"),
                errors=(),
                verified_postconditions=("hccast-absent", "stock-gadget-owns-udc"),
                success=True,
            )
        ]
    )

    assert supervisor.snapshot().phase is RuntimePhase.STARTING
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    assert factory.lifecycle[0] == ("reconcile", 1)
    assert factory.created == []
    supervisor.close()


def test_startup_active_reconciles_stale_gadget_before_first_attempt() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.KIOSK),
        scripts=[FakeAttemptScript(block_until_cancelled=True)],
    )

    supervisor.start()
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    assert factory.lifecycle[0] == ("reconcile", 1)
    assert factory.lifecycle[1] == ("create", DesiredMode.KIOSK)
    supervisor.close()


def test_startup_reconciliation_failure_blocks_active_attempt() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        reconciliations=[_cleanup_failure()],
    )

    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.ERROR)

    assert factory.created == []
    assert "restore-stock-gadget: verification failed" in (supervisor.snapshot().last_error or "")
    supervisor.close()


def test_ten_cycle_mode_restart_stop_race_never_overlaps_attempts() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        scripts=[FakeAttemptScript(block_until_cancelled=True) for _ in range(10)]
    )
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)

    actions = (
        DesiredMode.DESKTOP,
        DesiredMode.KIOSK,
        "restart",
        DesiredMode.STOPPED,
        DesiredMode.DESKTOP,
        DesiredMode.KIOSK,
        "restart",
        DesiredMode.STOPPED,
        DesiredMode.DESKTOP,
        DesiredMode.KIOSK,
    )
    expected_attempts = 0
    for action in actions:
        if action == "restart":
            supervisor.restart()
            expected_attempts += 1
            _eventually(
                lambda: len(factory.created) == expected_attempts
                and factory.created[-1].started.is_set()
            )
        else:
            mode = cast(DesiredMode, action)
            supervisor.select_mode(mode)
            if mode is DesiredMode.STOPPED:
                _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.STOPPED)
            else:
                expected_attempts += 1
                _eventually(
                    lambda: len(factory.created) == expected_attempts
                    and factory.created[-1].started.is_set()
                )
        assert factory.max_active_attempts <= 1

    supervisor.close()
    assert factory.max_active_attempts == 1
    assert factory.active_attempts == 0


def test_run_exception_with_failed_stop_blocks_until_successful_new_generation() -> None:
    scripts = [
        FakeAttemptScript(
            run_error=RuntimeError("attempt run exploded"),
            stop_cleanup=_cleanup_failure("stop-attempt"),
        ),
        FakeAttemptScript(block_until_cancelled=True),
    ]
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        scripts=scripts,
        reconciliations=[
            _cleanup_ok(),
            _cleanup_ok(),
            _cleanup_failure("restart-reconciliation"),
            _cleanup_ok(),
        ],
    )
    supervisor.start()

    try:
        _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.ERROR)
        status = supervisor.snapshot()
        assert "stop-attempt: verification failed" in (status.last_error or "")
        assert len(factory.created) == 1

        supervisor.restart()
        _eventually(
            lambda: supervisor.snapshot().phase is RuntimePhase.ERROR
            and "restart-reconciliation" in (supervisor.snapshot().last_error or "")
        )
        assert len(factory.created) == 1

        supervisor.restart()
        _eventually(lambda: len(factory.created) == 2 and factory.created[1].started.is_set())
        assert factory.max_active_attempts == 1
    finally:
        supervisor.close()


def test_close_preserves_unresolved_failed_attempt_cleanup() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        scripts=[
            FakeAttemptScript(
                run_error=RuntimeError("attempt run exploded"),
                stop_cleanup=_cleanup_failure("stop-attempt"),
            )
        ],
        reconciliations=[_cleanup_ok(), _cleanup_ok(), _cleanup_ok()],
    )
    supervisor.start()
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.ERROR)

    result = supervisor.close()

    assert not result.success
    assert CleanupError(action="stop-attempt", message="verification failed") in result.errors
    assert supervisor.snapshot().phase is RuntimePhase.ERROR
    assert "stop-attempt: verification failed" in (supervisor.snapshot().last_error or "")
    assert len(factory.created) == 1


def test_unexpected_worker_failure_terminalizes_controller_and_close_is_cached() -> None:
    clock = FakeClock()
    store = FakeStore(LiveConfig(mode=DesiredMode.DESKTOP))
    factory = FakeAttemptFactory(
        clock,
        scripts=[FakeAttemptScript(result=_failure_result())],
    )
    wait_strategy = ExplodingWaitStrategy()
    supervisor = LiveSupervisor(
        store=store,
        attempt_factory=factory,
        clock=clock,
        wait_strategy=wait_strategy,
    )
    supervisor.start()

    assert wait_strategy.entered.wait(1.0)
    _eventually(lambda: supervisor.snapshot().phase is RuntimePhase.ERROR)

    try:
        for mutation in (
            lambda: supervisor.select_mode(DesiredMode.KIOSK),
            lambda: supervisor.update_config(fps=12),
            supervisor.restart,
            supervisor.start,
        ):
            with pytest.raises(RuntimeError, match="supervisor is closed"):
                mutation()
    finally:
        first = supervisor.close()

    second = supervisor.close()
    assert first == second
    assert not first.success
    assert CleanupError(action="supervisor-worker", message="worker wait exploded") in first.errors


def test_close_joins_failed_worker_even_when_terminal_result_is_already_cached() -> None:
    clock = FakeClock()
    factory = FakeAttemptFactory(
        clock,
        scripts=[FakeAttemptScript(result=_failure_result())],
    )
    supervisor = ExitGatedSupervisor(
        store=FakeStore(LiveConfig(mode=DesiredMode.DESKTOP)),
        attempt_factory=factory,
        clock=clock,
        wait_strategy=ExplodingWaitStrategy(),
    )
    supervisor.start()
    assert supervisor.closed_result_published.wait(1.0)

    close_results: list[CleanupResult] = []
    close_finished = threading.Event()

    def close_supervisor() -> None:
        close_results.append(supervisor.close())
        close_finished.set()

    closer = threading.Thread(target=close_supervisor)
    closer.start()
    _eventually(lambda: supervisor._close_lock.locked())
    assert not close_finished.is_set()

    supervisor.allow_worker_exit.set()
    closer.join(timeout=2.0)
    assert not closer.is_alive()
    assert close_finished.is_set()
    assert len(close_results) == 1
    assert not close_results[0].success
    assert supervisor.close() == close_results[0]


def test_stale_attempt_result_cannot_overwrite_new_generation_starting_status() -> None:
    clock = FakeClock()
    before_return = threading.Event()
    allow_return = threading.Event()
    store = FakeStore(LiveConfig(mode=DesiredMode.DESKTOP))
    factory = GatedReconcileFactory(
        clock,
        scripts=[
            FakeAttemptScript(
                result=_failure_result(),
                before_return=before_return,
                allow_return=allow_return,
            ),
            FakeAttemptScript(block_until_cancelled=True),
        ],
    )
    supervisor = LiveSupervisor(
        store=store,
        attempt_factory=factory,
        clock=clock,
        wait_strategy=FakeWaitStrategy(clock),
    )
    supervisor.start()
    assert before_return.wait(1.0)

    supervisor.select_mode(DesiredMode.KIOSK)
    assert supervisor.snapshot().phase is RuntimePhase.STARTING
    allow_return.set()
    assert factory.second_reconcile_entered.wait(1.0)

    try:
        status = supervisor.snapshot()
        assert status.desired_mode is DesiredMode.KIOSK
        assert status.phase is RuntimePhase.STARTING
    finally:
        factory.allow_second_reconcile.set()
        supervisor.close()


def test_stale_retry_publication_cannot_overwrite_new_generation_starting_status() -> None:
    clock = FakeClock()
    result = GatedFailureResult(_cleanup_ok())
    wait_strategy = TerminalWaitGate()
    factory = FakeAttemptFactory(
        clock,
        scripts=[
            FakeAttemptScript(result=result),
            FakeAttemptScript(block_until_cancelled=True),
        ],
    )
    supervisor = LiveSupervisor(
        store=FakeStore(LiveConfig(mode=DesiredMode.DESKTOP)),
        attempt_factory=factory,
        clock=clock,
        wait_strategy=wait_strategy,
    )
    supervisor.start()
    assert result.ready_for_generation_change.wait(1.0)

    supervisor.select_mode(DesiredMode.KIOSK)
    assert supervisor.snapshot().phase is RuntimePhase.STARTING
    result.allow_terminal_publication.set()

    try:
        _eventually(
            lambda: len(factory.created) == 2 or wait_strategy.entered.is_set()
        )
        status = supervisor.snapshot()
        assert len(factory.created) == 2
        assert status.desired_mode is DesiredMode.KIOSK
        assert status.phase is RuntimePhase.STARTING
    finally:
        wait_strategy.release()
        supervisor.close()


def test_barrier_concurrent_mutations_never_overlap_and_close_deterministically() -> None:
    supervisor, _, factory, _, _ = _supervisor(
        config=LiveConfig(mode=DesiredMode.DESKTOP),
        scripts=[FakeAttemptScript(block_until_cancelled=True) for _ in range(4)],
    )
    supervisor.start()
    _eventually(lambda: len(factory.created) == 1 and factory.created[0].started.is_set())

    barrier = threading.Barrier(4)
    failures: list[BaseException] = []
    failure_lock = threading.Lock()

    def call_after_barrier(operation: Callable[[], object]) -> None:
        try:
            barrier.wait()
            operation()
        except BaseException as error:
            with failure_lock:
                failures.append(error)

    callers = [
        threading.Thread(
            target=call_after_barrier,
            args=(lambda: supervisor.select_mode(DesiredMode.KIOSK),),
        ),
        threading.Thread(
            target=call_after_barrier,
            args=(lambda: supervisor.update_config(fps=12),),
        ),
        threading.Thread(target=call_after_barrier, args=(supervisor.restart,)),
    ]
    for caller in callers:
        caller.start()
    barrier.wait()
    for caller in callers:
        caller.join(timeout=2.0)

    assert all(not caller.is_alive() for caller in callers)
    assert failures == []
    result = supervisor.close()
    assert result.success
    assert supervisor.snapshot().phase is RuntimePhase.STOPPED
    assert factory.max_active_attempts == 1
    assert factory.active_attempts == 0
