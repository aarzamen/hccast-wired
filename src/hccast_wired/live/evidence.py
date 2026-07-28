"""Private, durable evidence written for a single live-controller attempt."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
from typing import Callable, Literal
from urllib.parse import urlsplit, urlunsplit

from hccast_wired.live.model import LiveConfig
from hccast_wired.live.supervisor import AttemptEvent, AttemptResult


_ATTEMPT_TOKEN = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_ATTEMPT_DIRECTORY = re.compile(r"\d{8}T\d{6}Z-[A-Za-z0-9_-]{1,64}\Z")
_LogName = Literal["source", "encoder", "gadget"]


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("utc_now must return a timezone-aware datetime")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _normalized_absolute_root(root: Path) -> Path:
    """Return the exact safe root used for both validation and filesystem access."""

    if not root.is_absolute():
        raise ValueError("evidence root must be absolute")
    normalized = Path(os.path.normpath(root))
    if ".." in root.parts or normalized != root:
        raise ValueError("evidence root must be lexically normalized without '.' or '..'")
    return normalized


def _reject_symlink_ancestry(path: Path, *, label: str) -> None:
    """Reject any existing symlink component without resolving the requested path."""

    current = path
    while True:
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            pass
        else:
            if stat.S_ISLNK(mode):
                raise ValueError(f"{label} ancestry must not contain a symlink")
        parent = current.parent
        if parent == current:
            return
        current = parent


def _private_directory(path: Path, *, label: str) -> None:
    """Create or validate one owned directory without following a final symlink."""

    _reject_symlink_ancestry(path, label=label)
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as error:
        raise ValueError(f"{label} must be a directory") from error
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError as error:
        raise ValueError(f"{label} disappeared while being created") from error
    if stat.S_ISLNK(mode):
        raise ValueError(f"{label} must not be a symlink")
    if not stat.S_ISDIR(mode):
        raise ValueError(f"{label} must be a directory")
    os.chmod(path, 0o700)


def _redacted_config(config: LiveConfig) -> dict[str, object]:
    values = config.to_mapping()
    kiosk_url = values.get("kiosk_url")
    if not isinstance(kiosk_url, str):
        raise ValueError("kiosk_url must be a string")
    try:
        parts = urlsplit(kiosk_url)
    except ValueError as error:
        raise ValueError("kiosk_url is invalid") from error
    if parts.username is not None or parts.password is not None:
        raise ValueError("kiosk_url credentials are not permitted in evidence")
    values["kiosk_url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    return values


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode(
        "utf-8"
    )


class RunEvidenceWriter:
    """Thread-safe files for one attempt, including safe bounded retention."""

    def __init__(
        self,
        *,
        root: Path,
        path: Path,
        retention_count: int,
        retention_bytes: int,
        utc_now: Callable[[], datetime],
    ) -> None:
        self.root = root
        self.path = path
        self._retention_count = retention_count
        self._retention_bytes = retention_bytes
        self._utc_now = utc_now
        self._lock = threading.Lock()
        self._finalized = False

    @classmethod
    def start(
        cls,
        root: Path,
        config: LiveConfig,
        *,
        utc_now: Callable[[], datetime],
        attempt_token: str,
    ) -> RunEvidenceWriter:
        """Create one private attempt directory and its redacted effective config."""

        if _ATTEMPT_TOKEN.fullmatch(attempt_token) is None:
            raise ValueError("attempt_token must contain only letters, digits, underscores, or hyphens")
        redacted_config = _redacted_config(config)
        evidence_root = _normalized_absolute_root(Path(root))
        _private_directory(evidence_root, label="evidence root")
        attempt_path = evidence_root / f"{_format_utc(utc_now())}-{attempt_token}"
        _reject_symlink_ancestry(attempt_path, label="evidence attempt path")
        try:
            attempt_path.mkdir(mode=0o700)
        except FileExistsError as error:
            try:
                existing_mode = os.lstat(attempt_path).st_mode
            except FileNotFoundError:
                raise error
            if stat.S_ISLNK(existing_mode):
                raise ValueError("evidence attempt path must not be a symlink") from error
            raise
        _private_directory(attempt_path, label="evidence attempt path")
        writer = cls(
            root=evidence_root,
            path=attempt_path,
            retention_count=config.run_retention_count,
            retention_bytes=config.run_retention_bytes,
            utc_now=utc_now,
        )
        writer._atomic_json("effective-config.json", redacted_config)
        return writer

    def record_transition(self, event: AttemptEvent) -> None:
        """Append a timestamped externally visible lifecycle transition."""

        self._append_bytes(
            "transitions.jsonl",
            _json_bytes(
                {
                    "at": _format_utc(self._utc_now()),
                    "phase": event.phase.value,
                    "product": event.product,
                    "version": event.version,
                }
            ),
        )

    def append_log(self, stream: _LogName, data: str | bytes) -> None:
        """Append a decoded observation copy without rewriting any previous log data."""

        if stream not in {"source", "encoder", "gadget"}:
            raise ValueError("stream must be source, encoder, or gadget")
        if isinstance(data, str):
            encoded = data.encode("utf-8", errors="replace")
        elif isinstance(data, bytes):
            encoded = data
        else:
            raise TypeError("log data must be str or bytes")
        self._append_bytes(f"{stream}.log", encoded)

    def finalize(self, result: AttemptResult, *, terminal_reason: str) -> None:
        """Atomically mark this attempt complete, then prune only older safe attempts."""

        if not terminal_reason:
            raise ValueError("terminal_reason must not be empty")
        with self._lock:
            if self._finalized:
                raise RuntimeError("evidence attempt is already finalized")
            self._atomic_json(
                "RESULT.json",
                {
                    "classification": result.classification.value,
                    "cleanup": {
                        "attempted_actions": list(result.cleanup.attempted_actions),
                        "errors": [
                            {"action": error.action, "message": error.message}
                            for error in result.cleanup.errors
                        ],
                        "success": result.cleanup.success,
                        "verified_postconditions": list(result.cleanup.verified_postconditions),
                    },
                    "completed_at": _format_utc(self._utc_now()),
                    "error": result.error,
                    "product": result.product,
                    "streaming_duration": result.streaming_duration,
                    "terminal_reason": terminal_reason,
                    "version": result.version,
                },
            )
            self._finalized = True
            self._prune_complete_attempts()

    def _atomic_json(self, filename: str, value: object) -> None:
        destination = self.path / filename
        temporary = self.path / f".{filename}.{secrets.token_hex(12)}.tmp"
        payload = _json_bytes(value)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            os.chmod(destination, 0o600)
        except BaseException:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise

    def _append_bytes(self, filename: str, payload: bytes) -> None:
        path = self.path / filename
        with self._lock:
            try:
                existing_mode = os.lstat(path).st_mode
            except FileNotFoundError:
                existing_mode = None
            if existing_mode is not None and stat.S_ISLNK(existing_mode):
                raise ValueError(f"evidence file {filename} must not be a symlink")
            descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                offset = 0
                while offset < len(payload):
                    written = os.write(descriptor, payload[offset:])
                    if written <= 0:
                        raise OSError("could not append evidence log")
                    offset += written
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)

    def _prune_complete_attempts(self) -> None:
        complete = self._complete_attempts()
        complete_count = len(complete)
        total_bytes = sum(size for _, size in complete)
        for candidate, size in complete:
            if complete_count <= self._retention_count and total_bytes <= self._retention_bytes:
                break
            if candidate == self.path:
                continue
            self._remove_tree(candidate)
            complete_count -= 1
            total_bytes -= size

    def _complete_attempts(self) -> list[tuple[Path, int]]:
        complete: list[tuple[Path, int]] = []
        with os.scandir(self.root) as entries:
            for entry in entries:
                if _ATTEMPT_DIRECTORY.fullmatch(entry.name) is None:
                    continue
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    attempt = Path(entry.path)
                    result_mode = os.lstat(attempt / "RESULT.json").st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(result_mode):
                    continue
                complete.append((attempt, self._tree_size(attempt)))
        return sorted(complete, key=lambda item: item[0].name)

    def _tree_size(self, directory: Path) -> int:
        total = 0
        with os.scandir(directory) as entries:
            for entry in entries:
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISREG(entry_stat.st_mode):
                    total += entry_stat.st_size
                elif stat.S_ISDIR(entry_stat.st_mode):
                    total += self._tree_size(Path(entry.path))
        return total

    def _remove_tree(self, directory: Path) -> None:
        """Remove a verified directory tree without ever traversing a symlink."""

        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                entry_stat = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(entry_stat.st_mode):
                    self._remove_tree(path)
                else:
                    path.unlink()
        directory.rmdir()
