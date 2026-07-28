from __future__ import annotations

from pathlib import Path
import stat

import pytest

from hccast_wired.live.model import LiveConfig
from hccast_wired.live.store import LiveStateStore, StateStoreError


def test_missing_state_returns_stopped_defaults(tmp_path: Path) -> None:
    assert LiveStateStore(tmp_path / "state.json").load() == LiveConfig()


def test_dangling_state_symlink_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.symlink_to(tmp_path / "missing-state.json")

    with pytest.raises(StateStoreError, match="invalid state"):
        LiveStateStore(path).load()


def test_state_symlink_to_regular_file_fails_closed(tmp_path: Path) -> None:
    target = tmp_path / "actual-state.json"
    target.write_text("{}")
    path = tmp_path / "state.json"
    path.symlink_to(target)

    with pytest.raises(StateStoreError, match="invalid state"):
        LiveStateStore(path).load()


def test_save_round_trips_and_uses_private_permissions(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    config = LiveConfig().with_updates(mode="kiosk", kiosk_url="http://localhost:8080")

    LiveStateStore(path).save(config)

    assert LiveStateStore(path).load() == config
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(tmp_path.glob(".state.json.*")) == []


def test_invalid_existing_json_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"mode":"launch-shell"}')

    with pytest.raises(StateStoreError, match="invalid state"):
        LiveStateStore(path).load()


def test_invalid_state_does_not_replace_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    invalid_state = '{"mode":"launch-shell"}'
    path.write_text(invalid_state)

    with pytest.raises(StateStoreError, match="invalid state"):
        LiveStateStore(path).load()

    assert path.read_text() == invalid_state
