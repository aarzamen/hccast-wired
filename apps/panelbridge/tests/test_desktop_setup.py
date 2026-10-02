"""Transactional user configuration, preserving changes outside app blocks."""

from pathlib import Path
import base64
import configparser
import hashlib
import json
import os
import sys
import tempfile
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools.desktop_setup import DesktopSetup
from packaging_tools.user_setup import UserSetup

BASE = b"XKB_DEFAULT_LAYOUT=us\n"
K = b"profile {\n output HDMI-A-1 position 0,0\n}\n"
OUTPUT = [
    {
        "name": "HDMI-A-1",
        "enabled": True,
        "modes": [{"width": 400, "height": 1280, "refresh": 59.506, "current": True}],
        "position": {"x": 0, "y": 0},
        "transform": "270",
        "scale": 1,
    }
]

AUTOSTART_PATH = "autostart/panelbridge-session.desktop"
LEGACY_AUTOSTART = b"""[Desktop Entry]
Type=Application
Name=PanelBridge desktop session
Exec=/usr/lib/panelbridge/session-launch
Terminal=false
NoDisplay=true
OnlyShowIn=labwc;LXDE;
X-GNOME-Autostart-enabled=true
"""
CURRENT_AUTOSTART = b"""[Desktop Entry]
Type=Application
Name=PanelBridge desktop session
Exec=/usr/lib/panelbridge/session-launch
Terminal=false
NoDisplay=true
OnlyShowIn=labwc;LXDE;rpd-wayland;
X-GNOME-Autostart-enabled=true
"""


def encoded(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def legacy_install(base, before=None):
    """Build an old v2 fixture independently of the current autostart constant."""
    prepare(base).install(OUTPUT)
    setup = UserSetup(base)
    target = base / AUTOSTART_PATH
    target.parent.mkdir(mode=0o700)
    target.write_bytes(LEGACY_AUTOSTART)
    target.chmod(0o640)
    record = json.loads(setup.journal.read_text())
    record["files"][AUTOSTART_PATH] = {
        "before": encoded(before), "applied": encoded(LEGACY_AUTOSTART), "block": None,
        "mode": 0o640, "gid": target.stat().st_gid,
        "sha256": hashlib.sha256(LEGACY_AUTOSTART).hexdigest(),
    }
    if before is None:
        info = target.parent.stat()
        record["created_dirs"]["autostart"] = {"device": info.st_dev, "inode": info.st_ino}
    setup.journal.write_text(json.dumps(record))
    return setup, record


def assert_legacy_restored(base, before):
    assert (base / "labwc/environment").read_bytes() == BASE
    assert (base / "kanshi/config").read_bytes() == K
    target = base / AUTOSTART_PATH
    if before is None:
        assert not target.exists()
    else:
        assert target.read_bytes() == before
        assert target.stat().st_mode & 0o777 == 0o640


@pytest.mark.parametrize("desktop", ["labwc", "LXDE", "rpd-wayland"])
def test_installed_and_packaged_autostart_admit_supported_desktops(tmp_path, desktop):
    UserSetup(tmp_path).install([])
    package = Path(__file__).resolve().parents[1] / "packaging/panelbridge-session.desktop"
    for path in (tmp_path / AUTOSTART_PATH, package):
        entry = configparser.ConfigParser()
        entry.read_string(path.read_text())
        assert desktop in entry["Desktop Entry"]["OnlyShowIn"].split(";")
        assert entry["Desktop Entry"]["Exec"] == "/usr/lib/panelbridge/session-launch"


@pytest.mark.parametrize("before", [None, LEGACY_AUTOSTART])
def test_legacy_journal_status_and_restore_use_recorded_payload(tmp_path, before):
    setup, _ = legacy_install(tmp_path, before)
    journal = setup.journal.read_bytes()
    assert setup.status()["config_prepared"]
    assert setup.journal.read_bytes() == journal
    setup.uninstall()
    assert_legacy_restored(tmp_path, before)


@pytest.mark.parametrize("before", [None, LEGACY_AUTOSTART])
def test_legacy_upgrade_preserves_first_baseline_and_outside_edits(tmp_path, before):
    setup, original = legacy_install(tmp_path, before)
    env = tmp_path / "labwc/environment"
    env.write_bytes(env.read_bytes() + b"USER_SETTING=1\n")
    assert setup.install(OUTPUT)["config_prepared"]
    assert (tmp_path / AUTOSTART_PATH).read_bytes() == CURRENT_AUTOSTART
    updated = json.loads(setup.journal.read_text())
    assert updated["created_dirs"] == original["created_dirs"]
    for relative, item in original["files"].items():
        for field in ("before", "mode", "gid", "block"):
            assert updated["files"][relative][field] == item[field]
    journal = setup.journal.read_bytes()
    setup.install([])
    assert setup.journal.read_bytes() == journal
    setup.uninstall()
    assert env.read_bytes() == BASE + b"USER_SETTING=1\n"
    env.write_bytes(BASE)
    assert_legacy_restored(tmp_path, before)


def test_preexisting_unjournaled_legacy_autostart_is_upgraded_then_restored(tmp_path):
    prepare(tmp_path)
    target = tmp_path / AUTOSTART_PATH
    target.parent.mkdir()
    target.write_bytes(LEGACY_AUTOSTART)
    target.chmod(0o640)
    setup = UserSetup(tmp_path)
    setup.install(OUTPUT)
    assert target.read_bytes() == CURRENT_AUTOSTART
    setup.uninstall()
    assert_legacy_restored(tmp_path, LEGACY_AUTOSTART)


@pytest.mark.parametrize("before", [None, LEGACY_AUTOSTART])
@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("resume", ["install", "uninstall"])
def test_interrupted_legacy_upgrade_can_resume_or_restore(tmp_path, monkeypatch, before, after_write, resume):
    setup, _ = legacy_install(tmp_path, before)
    write = setup._write_target

    def interrupted(relative, *args):
        assert relative == AUTOSTART_PATH
        if after_write:
            write(relative, *args)
        raise OSError("injected upgrade write failure")

    monkeypatch.setattr(setup, "_write_target", interrupted)
    with pytest.raises(OSError, match="injected upgrade"):
        setup.install(OUTPUT)
    fresh = UserSetup(tmp_path)
    assert fresh.status()["state"] == "prepared"
    assert fresh.status()["conflicts"] == []
    if resume == "install":
        fresh.install([])
        assert (tmp_path / AUTOSTART_PATH).read_bytes() == CURRENT_AUTOSTART
    fresh.uninstall()
    assert_legacy_restored(tmp_path, before)


@pytest.mark.parametrize("phase", ["prepared", "installed"])
@pytest.mark.parametrize("after_save", [False, True])
@pytest.mark.parametrize("resume", ["install", "uninstall"])
def test_upgrade_journal_commit_failure_preserves_recovery(tmp_path, monkeypatch, phase, after_save, resume):
    setup, _ = legacy_install(tmp_path)
    save = setup._save

    def interrupted(record):
        if after_save:
            save(record)
        if record["state"] == phase:
            raise OSError("injected upgrade journal failure")
        if not after_save:
            save(record)

    monkeypatch.setattr(setup, "_save", interrupted)
    with pytest.raises(OSError, match="injected upgrade"):
        setup.install(OUTPUT)
    fresh = UserSetup(tmp_path)
    assert fresh.status()["conflicts"] == []
    if resume == "install":
        fresh.install([])
        assert (tmp_path / AUTOSTART_PATH).read_bytes() == CURRENT_AUTOSTART
    fresh.uninstall()
    assert_legacy_restored(tmp_path, None)


@pytest.mark.parametrize("change", ["autostart", "autostart_mode", "desktop_block"])
def test_legacy_upgrade_conflict_preserves_every_file_and_journal(tmp_path, change):
    setup, _ = legacy_install(tmp_path)
    target = tmp_path / AUTOSTART_PATH
    if change == "autostart":
        target.write_bytes(LEGACY_AUTOSTART + b"# user edit\n")
    elif change == "autostart_mode":
        target.chmod(0o600)
    else:
        target = tmp_path / "labwc/environment"
        target.write_bytes(target.read_bytes().replace(b"WLR_HEADLESS_OUTPUTS=1", b"WLR_HEADLESS_OUTPUTS=2"))
    snapshot = {p: (p.read_bytes(), p.stat().st_mode) for p in tmp_path.rglob("*") if p.is_file()}
    for action in (lambda: setup.install([]), setup.uninstall):
        with pytest.raises(RuntimeError, match="conflict|changed"):
            action()
        assert {p: (p.read_bytes(), p.stat().st_mode) for p in snapshot} == snapshot


@pytest.mark.parametrize("after_write", [False, True])
def test_legacy_restore_interruption_resumes_with_old_journal(tmp_path, monkeypatch, after_write):
    setup, _ = legacy_install(tmp_path)
    write = setup._write_target

    def interrupted(relative, *args):
        if relative != AUTOSTART_PATH or after_write:
            write(relative, *args)
        if relative == AUTOSTART_PATH:
            raise OSError("injected legacy restoration failure")

    monkeypatch.setattr(setup, "_write_target", interrupted)
    with pytest.raises(OSError, match="injected legacy"):
        setup.uninstall()
    fresh = UserSetup(tmp_path)
    assert fresh.status()["state"] == "restoring"
    fresh.uninstall()
    assert_legacy_restored(tmp_path, None)


def test_finished_upgrade_rejects_unjournaled_downgrade(tmp_path):
    setup, _ = legacy_install(tmp_path)
    setup.install([])
    target = tmp_path / AUTOSTART_PATH
    target.write_bytes(LEGACY_AUTOSTART)
    journal = setup.journal.read_bytes()
    assert setup.status()["conflicts"] == [AUTOSTART_PATH]
    for action in (lambda: setup.install([]), setup.uninstall):
        with pytest.raises(RuntimeError, match="conflict|changed"):
            action()
        assert target.read_bytes() == LEGACY_AUTOSTART
        assert setup.journal.read_bytes() == journal


def prepare(tmp_path):
    (tmp_path / "labwc").mkdir()
    (tmp_path / "kanshi").mkdir()
    (tmp_path / "labwc/environment").write_bytes(BASE)
    (tmp_path / "kanshi/config").write_bytes(K)
    return DesktopSetup(tmp_path)


def test_install_and_uninstall_restore_original_bytes_and_modes(tmp_path):
    setup = prepare(tmp_path)
    (tmp_path / "labwc/environment").chmod(0o640)
    setup.install(OUTPUT)
    assert b"WLR_HEADLESS_OUTPUTS=1" in (tmp_path / "labwc/environment").read_bytes()
    installed = (tmp_path / "kanshi/config").read_text()
    assert installed.startswith(K.decode())
    assert "400x1280@59.506" in installed and "transform 270" in installed
    assert "HEADLESS-1" in installed and "position 1280,0" in installed
    setup.uninstall()
    assert (tmp_path / "labwc/environment").read_bytes() == BASE
    assert (tmp_path / "labwc/environment").stat().st_mode & 0o777 == 0o640
    assert (tmp_path / "kanshi/config").read_bytes() == K


def test_user_edits_survive_and_repeated_install_is_idempotent(tmp_path):
    setup = prepare(tmp_path)
    setup.install(OUTPUT)
    path = tmp_path / "labwc/environment"
    once = path.read_bytes()
    setup.install(OUTPUT)
    assert path.read_bytes() == once
    path.write_bytes(once + b"USER_ADDED=1\n")
    setup.uninstall()
    assert path.read_bytes() == BASE + b"USER_ADDED=1\n"


def test_conflicting_backend_or_symlink_never_gets_overwritten(tmp_path):
    setup = prepare(tmp_path)
    path = tmp_path / "labwc/environment"
    path.write_bytes(b"WLR_BACKENDS=custom\n")
    with pytest.raises(RuntimeError, match="backend"):
        setup.install(OUTPUT)
    assert path.read_bytes() == b"WLR_BACKENDS=custom\n"
    path.unlink()
    path.symlink_to(tmp_path / "original")
    (tmp_path / "original").write_bytes(BASE)
    with pytest.raises(RuntimeError, match="symlink"):
        setup.install(OUTPUT)
    assert (tmp_path / "original").read_bytes() == BASE


def test_no_hdmi_still_installs_a_headless_profile(tmp_path):
    setup = prepare(tmp_path)
    setup.install([])
    text = (tmp_path / "kanshi/config").read_text()
    assert "output HEADLESS-1" in text and "position 0,0" in text


def test_private_home_allows_existing_group_writable_config_and_restores_it(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = home / ".config"
    config.mkdir(mode=0o775)
    setup = prepare(config)
    config.chmod(0o775)
    (config / "labwc").chmod(0o775)
    (config / "kanshi").chmod(0o775)
    setup.install(OUTPUT)
    assert b"WLR_HEADLESS_OUTPUTS=1" in (config / "labwc/environment").read_bytes()
    setup.uninstall()
    assert (config / "labwc/environment").read_bytes() == BASE
    assert (config / "kanshi/config").read_bytes() == K
    assert config.stat().st_mode & 0o777 == 0o775
    assert (config / "kanshi").stat().st_mode & 0o777 == 0o775


def test_exposing_private_home_stops_restoration_before_mutation():
    with tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp")) as root:
        public = Path(root)
        public.chmod(0o755)
        home = public / "home"
        home.mkdir(mode=0o700)
        config = home / ".config"
        config.mkdir(mode=0o775)
        setup = prepare(config)
        config.chmod(0o775)
        (config / "kanshi").chmod(0o775)
        setup.install(OUTPUT)
        installed = (config / "kanshi/config").read_bytes()
        journal = (config / "panelbridge/desktop-install.json").read_bytes()
        with setup._transaction(create=False) as files:
            assert files.read("kanshi/config")["data"] == installed
            home.chmod(0o755)
            with pytest.raises(RuntimeError, match="protected"):
                files.read("kanshi/config")
        home.chmod(0o755)
        with pytest.raises(RuntimeError, match="protected"):
            setup.uninstall()
        assert (config / "kanshi/config").read_bytes() == installed
        assert (config / "panelbridge/desktop-install.json").read_bytes() == journal
        home.chmod(0o700)
        setup.uninstall()
        assert (config / "kanshi/config").read_bytes() == K


def test_group_writable_config_without_private_ancestor_is_rejected():
    with tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp")) as root:
        home = Path(root)
        home.chmod(0o755)
        config = home / ".config"
        config.mkdir(mode=0o775)
        config.chmod(0o775)
        with pytest.raises(RuntimeError, match="protected"):
            DesktopSetup(config).install(OUTPUT)
        assert not (config / "panelbridge").exists()
