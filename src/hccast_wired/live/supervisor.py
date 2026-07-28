"""Deterministic, single-worker supervision for one live display attempt."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import threading
import time
from typing import Callable, Protocol

from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase, RuntimeStatus


_RETRY_DELAYS = (2.0, 4.0, 8.0, 16.0, 30.0)
_RETRY_RESET_SECONDS = 60.0
_ATTEMPT_EVENT_PHASES = frozenset(
    {
        RuntimePhase.WAITING_FOR_SCREEN,
        RuntimePhase.HANDSHAKING,
        RuntimePhase.STREAMING,
    }
)


@dataclass(frozen=True, slots=True)
class CleanupError:
    """One failed cleanup action and its bounded diagnostic message."""

    action: str
    message: str

    def __post_init__(self) -> None:
        if not self.action:
            raise ValueError("cleanup error action must not be empty")
        if not self.message:
            raise ValueError("cleanup error message must not be empty")


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Actions, failures, and verified postconditions from physical cleanup."""

    attempted_actions: tuple[str, ...]
    errors: tuple[CleanupError, ...]
    verified_postconditions: tuple[str, ...]
    success: bool

    def __post_init__(self) -> None:
        if not isinstance(self.success, bool):
            raise ValueError("cleanup success must be a boolean")
        if self.success and self.errors:
            raise ValueError("successful cleanup cannot contain errors")


class AttemptClassification(str, Enum):
    """Terminal classification returned by one attempt."""

    FAILURE = "failure"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class AttemptEvent:
    """One externally meaningful phase observed during an active attempt."""

    phase: RuntimePhase
    product: str | None = None
    version: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in _ATTEMPT_EVENT_PHASES:
            raise ValueError("attempt event phase is not externally publishable")
        for name in ("product", "version"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")


@dataclass(frozen=True, slots=True)
class AttemptResult:
    """Terminal evidence from an attempt, including its own checked cleanup."""

    classification: AttemptClassification
    error: str | None
    product: str | None
    version: str | None
    streaming_duration: float
    cleanup: CleanupResult

    def __post_init__(self) -> None:
        if not isinstance(self.classification, AttemptClassification):
            try:
                classification = AttemptClassification(self.classification)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid attempt classification") from error
            object.__setattr__(self, "classification", classification)
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("attempt error must be a string or None")
        for name in ("product", "version"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")
        if (
            isinstance(self.streaming_duration, bool)
            or not isinstance(self.streaming_duration, (int, float))
            or self.streaming_duration < 0
        ):
            raise ValueError("streaming_duration must be a non-negative number")


class LiveAttempt(Protocol):
    """Command-independent lifecycle owned exclusively by the supervisor worker."""

    @property
    def started_monotonic(self) -> float:
        """Return the monotonic start time recorded by the attempt."""

    def run(
        self,
        publish: Callable[[AttemptEvent], None],
        interrupted: threading.Event,
    ) -> AttemptResult | None:
        """Run until completion, or return ``None`` after interruption."""

    def stop(self) -> CleanupResult:
        """Stop child work and return checked attempt-local cleanup evidence."""


class AttemptFactory(Protocol):
    """Create attempts and enforce the idempotent physical stopped invariant."""

    def create(self, config: LiveConfig) -> LiveAttempt:
        """Create one not-yet-running attempt for an active configuration."""

    def reconcile_stopped(self) -> CleanupResult:
        """Remove stale custom state, restore stock gadget, and verify ownership."""


class Clock(Protocol):
    """Time source used for runtime timestamps and deterministic tests."""

    def monotonic(self) -> float:
        """Return monotonic seconds."""

    def utc_now(self) -> datetime:
        """Return the current timezone-aware UTC time."""


class WaitStrategy(Protocol):
    """Interruptible retry wait boundary."""

    def wait(
        self,
        condition: threading.Condition,
        interrupted: Callable[[], bool],
        timeout: float,
    ) -> bool:
        """Return true when interrupted, or false after the timeout elapses."""


class _StateStore(Protocol):
    def load(self) -> LiveConfig: ...

    def save(self, config: LiveConfig) -> None: ...


class SystemClock:
    """Production clock implementation."""

    def monotonic(self) -> float:
        return time.monotonic()

    def utc_now(self) -> datetime:
        return datetime.now(timezone.utc)


class ConditionWaitStrategy:
    """Production wait that wakes immediately when the controller generation changes."""

    def wait(
        self,
        condition: threading.Condition,
        interrupted: Callable[[], bool],
        timeout: float,
    ) -> bool:
        with condition:
            return condition.wait_for(interrupted, timeout=timeout)


def _empty_cleanup() -> CleanupResult:
    return CleanupResult(attempted_actions=(), errors=(), verified_postconditions=(), success=True)


def _merge_cleanup(*results: CleanupResult) -> CleanupResult:
    return CleanupResult(
        attempted_actions=tuple(
            action for result in results for action in result.attempted_actions
        ),
        errors=tuple(error for result in results for error in result.errors),
        verified_postconditions=tuple(
            postcondition for result in results for postcondition in result.verified_postconditions
        ),
        success=all(result.success for result in results),
    )


def _exception_cleanup(action: str, error: BaseException) -> CleanupResult:
    message = str(error).strip() or error.__class__.__name__
    return CleanupResult(
        attempted_actions=(action,),
        errors=(CleanupError(action=action, message=message),),
        verified_postconditions=(),
        success=False,
    )


def _cleanup_error_text(result: CleanupResult) -> str:
    if result.errors:
        details = "; ".join(f"{error.action}: {error.message}" for error in result.errors)
        return f"cleanup failed: {details}"
    return "cleanup failed: required postconditions were not verified"


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("clock returned a naive UTC time")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class LiveSupervisor:
    """Own one worker, one attempt, persisted intent, retries, and physical cleanup."""

    def __init__(
        self,
        *,
        store: _StateStore,
        attempt_factory: AttemptFactory,
        clock: Clock | None = None,
        wait_strategy: WaitStrategy | None = None,
    ) -> None:
        config = store.load()
        self._store = store
        self._attempt_factory = attempt_factory
        self._clock = clock or SystemClock()
        self._wait_strategy = wait_strategy or ConditionWaitStrategy()
        self._condition = threading.Condition()
        self._close_lock = threading.Lock()
        self._config = config
        self._generation = 0
        self._closing = False
        self._started = False
        self._worker: threading.Thread | None = None
        self._current_attempt: LiveAttempt | None = None
        self._attempt_interrupt: threading.Event | None = None
        self._last_cleanup: CleanupResult | None = None
        self._unresolved_cleanup: CleanupResult | None = None
        self._closed_result: CleanupResult | None = None
        self._status = RuntimeStatus(
            desired_mode=config.mode,
            phase=RuntimePhase.STARTING,
        )

    def start(self) -> None:
        """Start the single supervisor worker exactly once."""

        with self._condition:
            if self._closing:
                raise RuntimeError("supervisor is closed")
            if self._started:
                return
            self._started = True
            self._worker = threading.Thread(
                target=self._worker_entry,
                name="hccast-live-supervisor",
            )
            self._worker.start()

    def select_mode(self, mode: DesiredMode) -> RuntimeStatus:
        """Persist and apply an explicitly selected operating mode."""

        if not isinstance(mode, DesiredMode):
            try:
                mode = DesiredMode(mode)
            except (TypeError, ValueError) as error:
                raise ValueError("mode is invalid") from error
        with self._condition:
            self._ensure_open_locked()
            replacement = self._config.with_updates(mode=mode)
            self._store.save(replacement)
            self._config = replacement
            self._generation += 1
            self._interrupt_attempt_locked()
            transition = (
                RuntimePhase.STOPPING if mode is DesiredMode.STOPPED else RuntimePhase.STARTING
            )
            self._status = replace(
                self._status,
                desired_mode=mode,
                phase=transition,
                next_retry_at=None,
                last_error=None,
            )
            self._condition.notify_all()
            return self._status

    def update_config(self, **changes: object) -> RuntimeStatus:
        """Validate and persist configuration, restarting an active changed mode."""

        if "mode" in changes:
            raise ValueError("mode must be changed with select_mode")
        with self._condition:
            self._ensure_open_locked()
            replacement = self._config.with_updates(**changes)
            if replacement == self._config:
                return self._status
            self._store.save(replacement)
            self._config = replacement
            if replacement.mode is not DesiredMode.STOPPED:
                self._generation += 1
                self._interrupt_attempt_locked()
                self._status = replace(
                    self._status,
                    desired_mode=replacement.mode,
                    phase=RuntimePhase.STARTING,
                    next_retry_at=None,
                    last_error=None,
                )
                self._condition.notify_all()
            return self._status

    def restart(self) -> RuntimeStatus:
        """Restart the active mode without changing or re-saving persistent intent."""

        with self._condition:
            self._ensure_open_locked()
            if self._config.mode is DesiredMode.STOPPED:
                return self._status
            self._generation += 1
            self._interrupt_attempt_locked()
            self._status = replace(
                self._status,
                phase=RuntimePhase.STARTING,
                next_retry_at=None,
                last_error=None,
            )
            self._condition.notify_all()
            return self._status

    def snapshot(self) -> RuntimeStatus:
        """Return the current immutable runtime status under the state lock."""

        with self._condition:
            return self._status

    def close(self) -> CleanupResult:
        """Stop work, restore stock state, join the worker, and return checked cleanup."""

        with self._close_lock:
            with self._condition:
                if self._closed_result is not None:
                    result = self._closed_result
                    worker = self._worker
                else:
                    result = None
                    self._closing = True
                    self._generation += 1
                    self._interrupt_attempt_locked()
                    self._status = replace(
                        self._status,
                        phase=RuntimePhase.STOPPING,
                        next_retry_at=None,
                    )
                    worker = self._worker
                    self._condition.notify_all()

            if result is not None:
                if worker is not None and worker is not threading.current_thread():
                    worker.join()
                return result

            if worker is None:
                result = self._reconcile_stopped()
                self._publish_closed(result)
                return result

            worker.join()
            with self._condition:
                if self._closed_result is None:
                    result = _exception_cleanup(
                        "supervisor-worker", RuntimeError("worker exited without cleanup result")
                    )
                    self._publish_closed_locked(result)
                assert self._closed_result is not None
                return self._closed_result

    def _worker_entry(self) -> None:
        try:
            self._worker_main()
        except BaseException as error:
            message = str(error).strip() or error.__class__.__name__
            with self._condition:
                self._closing = True
                self._generation += 1
                self._interrupt_attempt_locked()
                self._status = replace(
                    self._status,
                    phase=RuntimePhase.ERROR,
                    next_retry_at=None,
                    attempt_started_at=None,
                    last_error=f"supervisor worker failed: {message}",
                )
                self._condition.notify_all()
            failure = _exception_cleanup("supervisor-worker", error)
            reconciliation = self._reconcile_stopped()
            self._publish_closed(_merge_cleanup(failure, reconciliation))

    def _worker_main(self) -> None:
        applied_generation = -1
        retry_index = 0
        pending_cleanup = _empty_cleanup()
        pending_result: AttemptResult | None = None
        pending_generation: int | None = None

        while True:
            baseline = self._reconcile_stopped()
            checked_cleanup = _merge_cleanup(pending_cleanup, baseline)
            with self._condition:
                current_generation = self._generation
                current_config = self._config
                generation_changed = current_generation != applied_generation
                if generation_changed:
                    retry_index = 0
                applied_generation = current_generation

                if self._closing:
                    if self._unresolved_cleanup is not None:
                        checked_cleanup = _merge_cleanup(
                            self._unresolved_cleanup, checked_cleanup
                        )
                    self._publish_closed_locked(checked_cleanup)
                    return

                if not checked_cleanup.success:
                    if self._unresolved_cleanup is not None:
                        checked_cleanup = _merge_cleanup(
                            self._unresolved_cleanup, checked_cleanup
                        )
                    self._unresolved_cleanup = checked_cleanup
                    self._last_cleanup = checked_cleanup
                    self._publish_cleanup_error_locked(checked_cleanup)
                    pending_cleanup = _empty_cleanup()
                    pending_result = None
                    pending_generation = None
                    self._condition.wait_for(
                        lambda: self._closing or self._generation != applied_generation
                    )
                    continue

                self._unresolved_cleanup = None
                self._last_cleanup = checked_cleanup

                if pending_result is not None and pending_generation == current_generation:
                    result = pending_result
                    self._status = replace(
                        self._status,
                        product=result.product or self._status.product,
                        version=result.version or self._status.version,
                        last_error=result.error,
                    )
                else:
                    result = None

                pending_cleanup = _empty_cleanup()
                pending_result = None
                pending_generation = None

                if current_config.mode is DesiredMode.STOPPED:
                    self._status = RuntimeStatus(
                        desired_mode=DesiredMode.STOPPED,
                        phase=RuntimePhase.STOPPED,
                    )
                    self._condition.notify_all()
                    self._condition.wait_for(
                        lambda: self._closing or self._generation != applied_generation
                    )
                    continue

            if result is not None:
                if result.classification is not AttemptClassification.FAILURE:
                    with self._condition:
                        if self._closing or self._generation != applied_generation:
                            continue
                        self._status = replace(
                            self._status,
                            phase=RuntimePhase.ERROR,
                            last_error=result.error or "attempt ended unexpectedly",
                            next_retry_at=None,
                        )
                        self._condition.notify_all()
                        self._condition.wait_for(
                            lambda: self._closing or self._generation != applied_generation
                        )
                    continue
                if result.streaming_duration >= _RETRY_RESET_SECONDS:
                    retry_index = 0
                delay = _RETRY_DELAYS[min(retry_index, len(_RETRY_DELAYS) - 1)]
                with self._condition:
                    if self._closing or self._generation != applied_generation:
                        continue
                    self._status = replace(
                        self._status,
                        phase=RuntimePhase.RETRYING,
                        retry_count=retry_index + 1,
                        next_retry_at=_format_utc(
                            self._clock.utc_now() + timedelta(seconds=delay)
                        ),
                        attempt_started_at=None,
                    )
                    self._condition.notify_all()
                interrupted = self._wait_strategy.wait(
                    self._condition,
                    lambda: self._closing or self._generation != applied_generation,
                    delay,
                )
                if interrupted:
                    continue
                retry_index = min(retry_index + 1, len(_RETRY_DELAYS) - 1)
                with self._condition:
                    if self._closing or self._generation != applied_generation:
                        continue

            with self._condition:
                if self._closing or self._generation != applied_generation:
                    continue
                config = self._config

            try:
                attempt = self._attempt_factory.create(config)
            except BaseException as error:
                pending_result = AttemptResult(
                    classification=AttemptClassification.FAILURE,
                    error=str(error).strip() or error.__class__.__name__,
                    product=None,
                    version=None,
                    streaming_duration=0.0,
                    cleanup=_empty_cleanup(),
                )
                pending_generation = applied_generation
                continue

            interrupt = threading.Event()
            with self._condition:
                if self._closing or self._generation != applied_generation:
                    continue
                self._current_attempt = attempt
                self._attempt_interrupt = interrupt
                self._status = RuntimeStatus(
                    desired_mode=config.mode,
                    phase=RuntimePhase.STARTING,
                    retry_count=retry_index,
                    attempt_started_at=_format_utc(self._clock.utc_now()),
                )
                self._condition.notify_all()

            try:
                attempt_result = attempt.run(
                    lambda event: self._publish_attempt_event(
                        attempt, applied_generation, event
                    ),
                    interrupt,
                )
            except BaseException as error:
                stop_cleanup = self._stop_attempt(attempt)
                attempt_result = AttemptResult(
                    classification=AttemptClassification.FAILURE,
                    error=str(error).strip() or error.__class__.__name__,
                    product=None,
                    version=None,
                    streaming_duration=0.0,
                    cleanup=stop_cleanup,
                )
                pending_cleanup = stop_cleanup
            else:
                if attempt_result is None:
                    stop_cleanup = self._stop_attempt(attempt)
                    pending_cleanup = stop_cleanup
                else:
                    pending_cleanup = attempt_result.cleanup

            with self._condition:
                if self._current_attempt is attempt:
                    self._current_attempt = None
                    self._attempt_interrupt = None
                if (
                    attempt_result is not None
                    and not self._closing
                    and self._generation == applied_generation
                ):
                    self._status = replace(self._status, phase=RuntimePhase.STOPPING)
                self._condition.notify_all()

            if attempt_result is not None:
                pending_result = attempt_result
                pending_generation = applied_generation
            else:
                pending_result = None
                pending_generation = None

    def _publish_attempt_event(
        self,
        attempt: LiveAttempt,
        generation: int,
        event: AttemptEvent,
    ) -> None:
        with self._condition:
            if (
                self._closing
                or self._generation != generation
                or self._current_attempt is not attempt
            ):
                return
            self._status = replace(
                self._status,
                phase=event.phase,
                product=event.product or self._status.product,
                version=event.version or self._status.version,
            )
            self._condition.notify_all()

    def _stop_attempt(self, attempt: LiveAttempt) -> CleanupResult:
        try:
            return attempt.stop()
        except BaseException as error:
            return _exception_cleanup("stop-attempt", error)

    def _reconcile_stopped(self) -> CleanupResult:
        try:
            return self._attempt_factory.reconcile_stopped()
        except BaseException as error:
            return _exception_cleanup("reconcile-stopped", error)

    def _interrupt_attempt_locked(self) -> None:
        if self._attempt_interrupt is not None:
            self._attempt_interrupt.set()

    def _ensure_open_locked(self) -> None:
        if self._closing:
            raise RuntimeError("supervisor is closed")

    def _publish_cleanup_error_locked(self, result: CleanupResult) -> None:
        self._status = replace(
            self._status,
            desired_mode=self._config.mode,
            phase=RuntimePhase.ERROR,
            next_retry_at=None,
            attempt_started_at=None,
            last_error=_cleanup_error_text(result),
        )
        self._condition.notify_all()

    def _publish_closed(self, result: CleanupResult) -> None:
        with self._condition:
            self._publish_closed_locked(result)

    def _publish_closed_locked(self, result: CleanupResult) -> None:
        self._last_cleanup = result
        self._closed_result = result
        if result.success:
            self._status = replace(
                self._status,
                phase=RuntimePhase.STOPPED,
                next_retry_at=None,
                attempt_started_at=None,
                last_error=None,
            )
        else:
            self._publish_cleanup_error_locked(result)
        self._condition.notify_all()
