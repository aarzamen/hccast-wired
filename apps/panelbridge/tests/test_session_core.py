"""Real profile and recovery-state behavior, independent of Linux hardware."""

import json
import os
import stat
from pathlib import Path
import sys
import tempfile
import unittest
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.models import Profile
from panelbridge.state import StateStore


class ProfileTests(unittest.TestCase):
    def test_defaults_separate_capture_and_wire_rate(self):
        p = Profile.from_dict(dict(Profile().to_dict(), content_fps=15))
        self.assertEqual(p.content_fps, 15)
        self.assertEqual(p.wire_fps, 30)

    def test_unknown_and_unsafe_values_rejected(self):
        for changes in (
            {"content_fps": True},
            {"bitrate_kbps": 0},
            {"wire_fps": 60},
            {"source_width": 999999},
            {"rotation": 91},
            {"scale": float("nan")},
            {"command": "anything"},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                Profile.from_dict(dict(Profile().to_dict(), **changes))

    def test_complete_profile_required(self):
        with self.assertRaises(ValueError):
            Profile.from_dict({"source_width": 1280})


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settings.json"
        self.store = StateStore(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_unconfirmed_trial_never_becomes_saved_default(self):
        original = self.store.profile
        candidate = Profile.from_dict(dict(original.to_dict(), content_fps=15))
        self.store.begin_trial(candidate, now=10)
        restarted = StateStore(self.path)
        self.assertEqual(restarted.profile, original)
        self.assertIsNone(restarted.pending)
        self.assertTrue(restarted.recovered_trial)

    def test_confirm_before_deadline_persists_and_expired_confirm_reverts(self):
        candidate = Profile.from_dict(dict(self.store.profile.to_dict(), content_fps=15))
        self.store.begin_trial(candidate, now=10)
        self.assertTrue(self.store.confirm_trial(now=29))
        self.assertEqual(StateStore(self.path).profile, candidate)
        self.store.begin_trial(Profile(), now=30)
        self.assertFalse(self.store.confirm_trial(now=51))
        self.assertEqual(self.store.profile, candidate)
        self.assertIsNone(self.store.pending)

    def test_three_failures_select_known_good_without_losing_binding(self):
        self.store.select_device("02:00:00:00:00:01", "Synthetic receiver")
        for count in (1, 2, 3):
            self.assertEqual(self.store.record_failure(), count)
        self.assertTrue(self.store.recovery_required)
        reloaded = StateStore(self.path)
        self.assertEqual(reloaded.selected_device["address"], "02:00:00:00:00:01")
        self.assertTrue(reloaded.recovery_required)
        reloaded.record_success()
        self.assertFalse(reloaded.recovery_required)

    def test_corruption_preserves_original_and_recovers_defaults(self):
        self.path.write_text("{broken")
        recovered = StateStore(self.path)
        self.assertTrue(recovered.config_error)
        self.assertEqual(self.path.read_text(), "{broken")
        self.assertEqual(recovered.profile, Profile())
        with self.assertRaises(RuntimeError):
            recovered.save()

    def test_symlink_not_followed(self):
        target = Path(self.tmp.name) / "untouched"
        target.write_text("original")
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            StateStore(self.path)
        self.assertEqual(target.read_text(), "original")

    def test_current_config_has_private_permissions(self):
        self.store.save()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.path.read_text())["api_version"], 1)


if __name__ == "__main__":
    unittest.main()
def test_restore_defaults_preserves_corrupt_bytes_and_blocks_concurrent_edits(tmp_path):
    path = tmp_path / "state.json"
    original = b'{"interrupted":'
    path.write_bytes(original)
    store = StateStore(path)
    assert store.config_error
    backup = store.restore_defaults()
    assert backup.read_bytes() == original
    assert StateStore(path).profile == Profile()
    assert store.config_error is None
    store2 = StateStore(path)
    path.write_text('{"user_changed":true}')
    with pytest.raises(RuntimeError, match="another process"):
        store2.restore_defaults()
    assert path.read_text() == '{"user_changed":true}'


def test_failed_trial_save_preserves_current_profile_and_pending(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.begin_trial(Profile(content_fps=15), 100)
    path.write_text('{"external_edit":true}')
    with pytest.raises(RuntimeError, match="another process"):
        store.confirm_trial(101)
    assert store.profile == Profile()
    assert store.pending["profile"].content_fps == 15


@pytest.mark.parametrize("content", ["[]", "null", "true", "42", '"value"'])
def test_nonobject_config_remains_recoverable(tmp_path, content):
    path = tmp_path / "state.json"
    path.write_text(content)
    store = StateStore(path)
    assert store.config_error and store.profile == Profile()
    backup = store.restore_defaults()
    assert backup.read_text() == content
    assert StateStore(path).config_error is None


def test_directory_sync_failure_keeps_memory_aligned_with_replaced_file(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    store = StateStore(path)
    store.save()
    sync = os.fsync

    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("directory sync failed")
        return sync(fd)

    monkeypatch.setattr(os, "fsync", fail_directory)
    with pytest.raises(OSError, match="directory sync failed"):
        store.select_device("02:00:00:00:00:01", "Synthetic receiver")
    assert store.selected_device == StateStore(path).selected_device
    monkeypatch.setattr(os, "fsync", sync)
    store.save()  # Its own committed write must not look like an external edit.
    assert StateStore(path).selected_device == store.selected_device
