"""Validated, serializable configuration and runtime state for the live controller."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any, Mapping, cast
from urllib.parse import urlsplit


class DesiredMode(str, Enum):
    """Persistent modes accepted by the controller."""

    STOPPED = "stopped"
    DESKTOP = "desktop"
    KIOSK = "kiosk"


class RuntimePhase(str, Enum):
    """Observed state of the controller's current attempt."""

    STOPPED = "stopped"
    STARTING = "starting"
    WAITING_FOR_SCREEN = "waiting_for_screen"
    HANDSHAKING = "handshaking"
    STREAMING = "streaming"
    RETRYING = "retrying"
    STOPPING = "stopping"
    ERROR = "error"


_SOURCE_USER_PATTERN = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")


def _enum_value(value: object, enum_type: type[DesiredMode] | type[RuntimePhase], name: str) -> object:
    if isinstance(value, enum_type):
        return value
    if isinstance(value, str):
        try:
            return enum_type(value)
        except ValueError as error:
            raise ValueError(f"{name} is invalid") from error
    raise ValueError(f"{name} is invalid")


def _bounded_int(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _fixed_value(value: object, expected: object, name: str) -> object:
    if value != expected or type(value) is not type(expected):
        raise ValueError(f"{name} is fixed at {expected!r}")
    return expected


def _validate_kiosk_url(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("kiosk_url must be an HTTP or HTTPS URL")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as error:
        raise ValueError("kiosk_url must be an HTTP or HTTPS URL") from error
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.netloc.endswith(":")
        or parts.username is not None
        or parts.password is not None
    ):
        raise ValueError("kiosk_url must be an HTTP or HTTPS URL without credentials")
    return value


def _validate_source_user(value: object) -> str:
    if not isinstance(value, str) or _SOURCE_USER_PATTERN.fullmatch(value) is None:
        raise ValueError("source_user must be a conservative POSIX account name")
    return value


@dataclass(frozen=True, slots=True)
class LiveConfig:
    """Persistent controller configuration with fixed, local-only operational limits."""

    schema_version: int = 1
    mode: DesiredMode = DesiredMode.STOPPED
    kiosk_url: str = "http://127.0.0.1:3000"
    fps: int = 10
    bitrate_kbps: int = 4000
    source_user: str = "hccast"
    novnc_enabled: bool = True
    width: int = 640
    height: int = 1136
    display_number: int = 99
    controller_host: str = "127.0.0.1"
    controller_port: int = 8765
    novnc_host: str = "127.0.0.1"
    novnc_port: int = 6080
    run_retention_count: int = 20
    run_retention_bytes: int = 200 * 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_version", _fixed_value(self.schema_version, 1, "schema_version"))
        object.__setattr__(self, "mode", _enum_value(self.mode, DesiredMode, "mode"))
        object.__setattr__(self, "kiosk_url", _validate_kiosk_url(self.kiosk_url))
        object.__setattr__(self, "fps", _bounded_int(self.fps, "fps", 1, 30))
        object.__setattr__(self, "bitrate_kbps", _bounded_int(self.bitrate_kbps, "bitrate_kbps", 500, 16000))
        object.__setattr__(self, "source_user", _validate_source_user(self.source_user))
        if not isinstance(self.novnc_enabled, bool):
            raise ValueError("novnc_enabled must be a boolean")
        object.__setattr__(self, "width", _fixed_value(self.width, 640, "width"))
        object.__setattr__(self, "height", _fixed_value(self.height, 1136, "height"))
        object.__setattr__(self, "display_number", _fixed_value(self.display_number, 99, "display_number"))
        object.__setattr__(
            self, "controller_host", _fixed_value(self.controller_host, "127.0.0.1", "controller_host")
        )
        object.__setattr__(self, "controller_port", _fixed_value(self.controller_port, 8765, "controller_port"))
        object.__setattr__(self, "novnc_host", _fixed_value(self.novnc_host, "127.0.0.1", "novnc_host"))
        object.__setattr__(self, "novnc_port", _fixed_value(self.novnc_port, 6080, "novnc_port"))
        object.__setattr__(
            self, "run_retention_count", _fixed_value(self.run_retention_count, 20, "run_retention_count")
        )
        object.__setattr__(
            self,
            "run_retention_bytes",
            _fixed_value(self.run_retention_bytes, 200 * 1024 * 1024, "run_retention_bytes"),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> LiveConfig:
        """Build a configuration from the exact persisted/API JSON shape."""

        known_fields = set(cls.__dataclass_fields__)
        unknown_fields = set(value) - known_fields
        if unknown_fields:
            unknown = ", ".join(sorted(unknown_fields))
            raise ValueError(f"unknown configuration field(s): {unknown}")
        return cls(**cast(Any, dict(value)))

    def with_updates(self, **changes: object) -> LiveConfig:
        """Return a separately validated replacement with the requested changes."""

        values = self.to_mapping()
        values.update(changes)
        return self.from_mapping(values)

    def to_mapping(self) -> dict[str, object]:
        """Return the stable JSON-safe persisted configuration shape."""

        return {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "kiosk_url": self.kiosk_url,
            "fps": self.fps,
            "bitrate_kbps": self.bitrate_kbps,
            "source_user": self.source_user,
            "novnc_enabled": self.novnc_enabled,
            "width": self.width,
            "height": self.height,
            "display_number": self.display_number,
            "controller_host": self.controller_host,
            "controller_port": self.controller_port,
            "novnc_host": self.novnc_host,
            "novnc_port": self.novnc_port,
            "run_retention_count": self.run_retention_count,
            "run_retention_bytes": self.run_retention_bytes,
        }


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """JSON-safe snapshot of observed runtime state, separate from desired mode."""

    desired_mode: DesiredMode
    phase: RuntimePhase
    retry_count: int = 0
    next_retry_at: str | None = None
    attempt_started_at: str | None = None
    product: str | None = None
    version: str | None = None
    last_error: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "desired_mode", _enum_value(self.desired_mode, DesiredMode, "desired_mode"))
        object.__setattr__(self, "phase", _enum_value(self.phase, RuntimePhase, "phase"))
        object.__setattr__(self, "retry_count", _bounded_int(self.retry_count, "retry_count", 0, 2**31 - 1))
        for name in ("next_retry_at", "attempt_started_at", "product", "version", "last_error"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"{name} must be a string or None")

    def to_mapping(self) -> dict[str, object]:
        """Return only JSON-safe strings, integers, booleans, and ``None`` values."""

        return {
            "desired_mode": self.desired_mode.value,
            "phase": self.phase.value,
            "retry_count": self.retry_count,
            "next_retry_at": self.next_retry_at,
            "attempt_started_at": self.attempt_started_at,
            "product": self.product,
            "version": self.version,
            "last_error": self.last_error,
        }
