"""Fail-closed, atomic persistence for live-controller configuration."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile

from hccast_wired.live.model import LiveConfig


class StateStoreError(RuntimeError):
    """Raised when persisted state cannot be parsed or pass validation."""


class LiveStateStore:
    """Persist one validated configuration using a private atomic replacement."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> LiveConfig:
        """Return stopped defaults when absent, otherwise a fully validated configuration."""

        try:
            path_status = self._path.lstat()
        except FileNotFoundError:
            return LiveConfig()
        except OSError as error:
            raise StateStoreError("invalid state") from error
        if not stat.S_ISREG(path_status.st_mode):
            raise StateStoreError("invalid state")
        try:
            with self._path.open("r", encoding="utf-8") as state_file:
                value: object = json.load(state_file)
        except (OSError, json.JSONDecodeError) as error:
            raise StateStoreError("invalid state") from error
        if not isinstance(value, dict):
            raise StateStoreError("invalid state")
        try:
            return LiveConfig.from_mapping(value)
        except (TypeError, ValueError) as error:
            raise StateStoreError("invalid state") from error

    def save(self, config: LiveConfig) -> None:
        """Atomically replace state with UTF-8 JSON owned only by the controller user."""

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._path.parent,
                prefix=f".{self._path.name}.",
                delete=False,
            ) as state_file:
                temporary_path = Path(state_file.name)
                json.dump(config.to_mapping(), state_file, sort_keys=True, separators=(",", ":"))
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self._path)
        except BaseException:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
            raise
