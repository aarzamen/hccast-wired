"""User configuration transactions; no session, network or device operations."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from contextlib import contextmanager

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools.desktop_setup import DesktopSetup
from packaging_tools.user_setup import UserSetup

ENV = b"XKB_DEFAULT_LAYOUT=us\n"
KANSHI = b"profile {\n output HDMI-A-1 position 0,0\n}\n"
AUTOSTART_PATH = "autostart/panelbridge-session.desktop"
TARGETS = ("labwc/environment", "kanshi/config", AUTOSTART_PATH)
TEMPLATE = (Path(__file__).resolve().parents[1] / "packaging/panelbridge-session.desktop").read_bytes()


@contextmanager
def public_temp_directory():
    with tempfile.TemporaryDirectory(dir=os.path.realpath("/tmp")) as root:
        path = Path(root)
        path.chmod(0o755)
        yield path


def baseline(tmp_path):
    for relative, content, mode in ((TARGETS[0], ENV, 0o640), (TARGETS[1], KANSHI, 0o600)):
        path = tmp_path / relative
        path.parent.mkdir()
        path.write_bytes(content)
        path.chmod(mode)
    return UserSetup(tmp_path)


def assert_restored(base):
    assert (base / TARGETS[0]).read_bytes() == ENV
    assert stat.S_IMODE((base / TARGETS[0]).stat().st_mode) == 0o640
    assert (base / TARGETS[1]).read_bytes() == KANSHI
    assert not (base / AUTOSTART_PATH).exists()


def test_config_and_autostart_round_trip_preserves_first_bytes_modes_and_edits(tmp_path):
    setup = baseline(tmp_path)
    setup.install([])
    assert (tmp_path / AUTOSTART_PATH).read_bytes() == TEMPLATE
    journal = setup.journal.read_bytes()
    env = tmp_path / TARGETS[0]
    env.write_bytes(env.read_bytes() + b"USER_SETTING=1\n")
    setup.install([])
    assert setup.journal.read_bytes() == journal
    assert setup.status()["state"] == "installed"
    setup.uninstall()
    assert env.read_bytes() == ENV + b"USER_SETTING=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert (tmp_path / TARGETS[1]).read_bytes() == KANSHI
    assert not (tmp_path / AUTOSTART_PATH).exists()
    assert setup.status()["state"] == "uninstalled"
    assert setup.status()["live_output_restored"] is None
    setup.uninstall()


def test_status_of_missing_config_is_read_only(tmp_path):
    base = tmp_path / "missing"
    assert UserSetup(base).status()["state"] == "not_installed"
    assert not base.exists()


def test_unrelated_autostart_prevents_all_config_mutations(tmp_path):
    setup = baseline(tmp_path)
    target = tmp_path / AUTOSTART_PATH
    target.parent.mkdir()
    target.write_bytes(b"[Desktop Entry]\nExec=/usr/bin/other-app\n")
    before = {name: (tmp_path / name).read_bytes() for name in TARGETS}
    with pytest.raises(RuntimeError, match="autostart"):
        setup.install([])
    assert {name: (tmp_path / name).read_bytes() for name in TARGETS} == before


def test_matching_preexisting_autostart_is_retained_with_original_mode(tmp_path):
    setup = baseline(tmp_path)
    target = tmp_path / AUTOSTART_PATH
    target.parent.mkdir()
    target.write_bytes(TEMPLATE)
    target.chmod(0o640)
    setup.install([])
    setup.uninstall()
    assert target.read_bytes() == TEMPLATE
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("after_write", [False, True])
def test_interrupted_install_resumes_without_replacing_first_originals(tmp_path, monkeypatch, boundary, after_write):
    setup = baseline(tmp_path)
    original = setup._write_target
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == boundary and not after_write:
            raise OSError("injected failure")
        result = original(*args, **kwargs)
        if count == boundary and after_write:
            raise OSError("injected failure after mutation")
        return result

    monkeypatch.setattr(setup, "_write_target", interrupted)
    with pytest.raises(OSError, match="injected"):
        setup.install([])
    resumed = UserSetup(tmp_path)
    assert resumed.status()["state"] == "prepared"
    resumed.install([])
    assert resumed.status()["state"] == "installed"
    resumed.uninstall()
    assert_restored(tmp_path)


@pytest.mark.parametrize("boundary", [1, 2, 3])
@pytest.mark.parametrize("after_write", [False, True])
def test_interrupted_uninstall_resumes_with_outside_edits(tmp_path, monkeypatch, boundary, after_write):
    setup = baseline(tmp_path)
    setup.install([])
    env = tmp_path / TARGETS[0]
    env.write_bytes(env.read_bytes() + b"USER_SETTING=1\n")
    original = setup._write_target
    count = 0

    def interrupted(*args, **kwargs):
        nonlocal count
        count += 1
        if count == boundary and not after_write:
            raise OSError("injected failure")
        result = original(*args, **kwargs)
        if count == boundary and after_write:
            raise OSError("injected failure after mutation")
        return result

    monkeypatch.setattr(setup, "_write_target", interrupted)
    with pytest.raises(OSError, match="injected"):
        setup.uninstall()
    resumed = UserSetup(tmp_path)
    assert resumed.status()["state"] == "restoring"
    resumed.uninstall()
    assert env.read_bytes() == ENV + b"USER_SETTING=1\n"
    assert (tmp_path / TARGETS[1]).read_bytes() == KANSHI
    assert not (tmp_path / AUTOSTART_PATH).exists()
    assert resumed.status()["state"] == "uninstalled"


@pytest.mark.parametrize("phase", ["prepared", "installed", "restoring", "uninstalled"])
@pytest.mark.parametrize("after_save", [False, True])
def test_journal_commit_failure_can_be_resumed(tmp_path, monkeypatch, phase, after_save):
    setup = baseline(tmp_path)
    installing = phase in ("prepared", "installed")
    if not installing:
        setup.install([])
    save = setup._save
    failed = False

    def interrupted(record):
        nonlocal failed
        if after_save:
            save(record)
        if not failed and record["state"] == phase:
            failed = True
            raise OSError("injected journal boundary")
        if not after_save:
            save(record)

    monkeypatch.setattr(setup, "_save", interrupted)
    with pytest.raises(OSError, match="journal boundary"):
        setup.install([]) if installing else setup.uninstall()
    resumed = UserSetup(tmp_path)
    if installing:
        resumed.install([])
    resumed.uninstall()
    assert_restored(tmp_path)


def test_conflict_in_last_target_is_detected_before_restoring_any_file(tmp_path):
    setup = baseline(tmp_path)
    setup.install([])
    target = tmp_path / AUTOSTART_PATH
    target.write_bytes(TEMPLATE + b"# changed by user\n")
    before = {name: (tmp_path / name).read_bytes() for name in TARGETS}
    with pytest.raises(RuntimeError, match="changed|conflict"):
        setup.uninstall()
    assert {name: (tmp_path / name).read_bytes() for name in TARGETS} == before
    assert AUTOSTART_PATH in setup.status()["conflicts"]


def test_install_does_not_reverse_an_interrupted_uninstall(tmp_path, monkeypatch):
    setup = baseline(tmp_path)
    setup.install([])
    monkeypatch.setattr(setup, "_write_target", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("stop")))
    with pytest.raises(OSError):
        setup.uninstall()
    with pytest.raises(RuntimeError, match="uninstall|restor"):
        UserSetup(tmp_path).install([])


@pytest.mark.parametrize("relative", [*TARGETS, "panelbridge/desktop-install.json", "panelbridge/desktop-install.lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_file_types_never_gain_configuration_authority(tmp_path, relative, kind):
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.write_bytes(b"preserve outside")
    if kind == "symlink":
        target.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, target)
    else:
        os.mkfifo(target)
    with pytest.raises(RuntimeError):
        UserSetup(tmp_path).install([])
    assert outside.read_bytes() == b"preserve outside"


@pytest.mark.parametrize("parent", ["labwc", "kanshi", "autostart", "panelbridge"])
def test_symlink_parent_is_never_followed(tmp_path, parent):
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / parent).symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError):
        UserSetup(tmp_path).install([])
    assert list(outside.iterdir()) == []


def test_directory_cleanup_only_removes_recorded_empty_directories(tmp_path):
    preexisting = tmp_path / "kanshi"
    preexisting.mkdir()
    setup = UserSetup(tmp_path)
    setup.install([])
    (tmp_path / "labwc/unrelated").write_bytes(b"keep")
    setup.uninstall()
    assert preexisting.is_dir()
    assert (tmp_path / "labwc/unrelated").read_bytes() == b"keep"
    assert not (tmp_path / "autostart").exists()
    assert setup.journal.is_file()


def test_replaced_directory_identity_is_not_removed(tmp_path):
    setup = UserSetup(tmp_path)
    setup.install([])
    (tmp_path / "autostart").rename(tmp_path / "old-autostart")
    (tmp_path / "autostart").mkdir()
    (tmp_path / AUTOSTART_PATH).write_bytes(TEMPLATE)
    (tmp_path / AUTOSTART_PATH).chmod(0o600)
    setup.uninstall()
    assert (tmp_path / "autostart").is_dir()
    assert (tmp_path / "old-autostart/panelbridge-session.desktop").is_file()


@pytest.mark.parametrize("field", ["files", "created_dirs"])
def test_forged_journal_cannot_nominate_an_arbitrary_path(tmp_path, field):
    setup = baseline(tmp_path)
    setup.install([])
    record = json.loads(setup.journal.read_text())
    record[field]["../outside"] = next(iter(record[field].values()))
    setup.journal.write_text(json.dumps(record))
    with pytest.raises(RuntimeError, match="journal"):
        setup.uninstall()


def test_truncated_or_oversized_journal_preserves_existing_config(tmp_path):
    setup = baseline(tmp_path)
    setup.install([])
    before = (tmp_path / TARGETS[0]).read_bytes()
    for data in (b"{", b"x" * (8 * 1024 * 1024 + 1)):
        setup.journal.write_bytes(data)
        with pytest.raises(RuntimeError):
            setup.uninstall()
        assert (tmp_path / TARGETS[0]).read_bytes() == before


@pytest.mark.parametrize("method", ["getuid", "geteuid"])
def test_real_or_effective_root_is_rejected_before_any_write(tmp_path, monkeypatch, method):
    monkeypatch.setattr("packaging_tools.desktop_setup.os." + method, lambda: 0)
    with pytest.raises(RuntimeError, match="root|unprivileged"):
        UserSetup(tmp_path).install([])
    assert list(tmp_path.iterdir()) == []


def test_desktop_only_install_can_be_extended_and_restored_by_user_setup(tmp_path):
    baseline(tmp_path)
    DesktopSetup(tmp_path).install([])
    setup = UserSetup(tmp_path)
    setup.install([])
    setup.uninstall()
    assert_restored(tmp_path)


def test_legacy_v1_desktop_receipt_retains_first_originals_on_extension(tmp_path):
    baseline(tmp_path)
    desktop = DesktopSetup(tmp_path)
    desktop.install([])
    record = json.loads(desktop.journal.read_bytes())
    record = {key: record[key] for key in ("api_version", "state", "files")}
    record["api_version"] = 1
    for item in record["files"].values():
        item.pop("gid")
    desktop.journal.write_text(json.dumps(record))
    UserSetup(tmp_path).install([])
    migrated = json.loads(desktop.journal.read_bytes())
    assert migrated["api_version"] == 2
    for relative, item in record["files"].items():
        assert migrated["files"][relative]["before"] == item["before"]
    UserSetup(tmp_path).uninstall()
    assert_restored(tmp_path)


def test_second_writer_cannot_modify_a_transaction_in_progress(tmp_path):
    setup = baseline(tmp_path)
    with setup._transaction(create=True):
        with pytest.raises(RuntimeError, match="transaction"):
            UserSetup(tmp_path).install([])
    assert not setup.journal.exists()
    assert_restored(tmp_path)


def test_mode_change_is_a_conflict_before_any_restore(tmp_path):
    setup = baseline(tmp_path)
    setup.install([])
    (tmp_path / AUTOSTART_PATH).chmod(0o640)
    before = {name: (tmp_path / name).read_bytes() for name in TARGETS}
    with pytest.raises(RuntimeError, match="conflict"):
        setup.uninstall()
    assert {name: (tmp_path / name).read_bytes() for name in TARGETS} == before
    assert (tmp_path / AUTOSTART_PATH).stat().st_mode & 0o777 == 0o640


def test_restore_journal_cannot_substitute_arbitrary_bytes(tmp_path, monkeypatch):
    setup = baseline(tmp_path)
    setup.install([])
    with monkeypatch.context() as patch:
        patch.setattr(setup, "_write_target", lambda *args: (_ for _ in ()).throw(OSError("fault")))
        with pytest.raises(OSError):
            setup.uninstall()
    record = json.loads(setup.journal.read_bytes())
    record["restore"][TARGETS[0]]["to"] = "cmVwbGFjZWQ="
    setup.journal.write_text(json.dumps(record))
    before = {name: (tmp_path / name).read_bytes() for name in TARGETS}
    with pytest.raises(RuntimeError, match="journal"):
        UserSetup(tmp_path).uninstall()
    assert {name: (tmp_path / name).read_bytes() for name in TARGETS} == before


@pytest.mark.parametrize("directory", [".", "labwc", "kanshi", "panelbridge", "autostart"])
def test_group_writable_configuration_authority_is_rejected(directory):
    with public_temp_directory() as root:
        path = root / directory
        path.mkdir(exist_ok=True)
        path.chmod(0o770)
        with pytest.raises(RuntimeError, match="director"):
            UserSetup(root).install([])
        assert not (root / AUTOSTART_PATH).exists()


def test_private_config_round_trip_with_linux_umask_0002_preserves_child_modes(tmp_path):
    previous_umask = os.umask(0o002)
    try:
        config = tmp_path / "config"
        config.mkdir(mode=0o700)
        setup = baseline(config)
        (config / "autostart").mkdir()
        (config / "panelbridge").mkdir()
        directories = ("labwc", "kanshi", "autostart", "panelbridge")
        assert stat.S_IMODE(config.stat().st_mode) == 0o700
        assert all(stat.S_IMODE((config / name).stat().st_mode) == 0o775
                   for name in directories)
        setup.install([])
        assert setup.status()["state"] == "installed"
        setup.uninstall()
        assert_restored(config)
        assert setup.status()["state"] == "uninstalled"
        assert stat.S_IMODE(config.stat().st_mode) == 0o700
        assert all(stat.S_IMODE((config / name).stat().st_mode) == 0o775
                   for name in directories)
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("root_mode,allowed", [(0o700, True), (0o744, True),
                                              (0o755, False), (0o710, False),
                                              (0o701, False), (0o770, False)])
def test_group_write_exception_requires_owned_outer_root_without_other_traversal(
        root_mode, allowed):
    with public_temp_directory() as root:
        root.chmod(root_mode)
        child = root / "kanshi"
        child.mkdir(mode=0o700)
        child.chmod(0o775)
        setup = UserSetup(root)
        if allowed:
            setup.install([])
            setup.uninstall()
        else:
            with pytest.raises(RuntimeError, match="director"):
                setup.install([])
            assert not (child / "config").exists()
        assert stat.S_IMODE(root.stat().st_mode) == root_mode
        assert stat.S_IMODE(child.stat().st_mode) == 0o775


@pytest.mark.parametrize("directory", ["labwc", "kanshi", "autostart", "panelbridge"])
def test_private_outer_root_never_allows_world_writable_child(tmp_path, directory):
    tmp_path.chmod(0o700)
    child = tmp_path / directory
    child.mkdir()
    child.chmod(0o777)
    with pytest.raises(RuntimeError, match="director"):
        UserSetup(tmp_path).install([])
    assert stat.S_IMODE(child.stat().st_mode) == 0o777


def test_private_directory_exception_is_rechecked_after_outer_permissions_change():
    with public_temp_directory() as root:
        root.chmod(0o700)
        child = root / "kanshi"
        child.mkdir()
        child.chmod(0o775)
        (child / "config").write_bytes(KANSHI)
        setup = UserSetup(root)
        with setup._transaction(create=True) as files:
            assert files.read("kanshi/config")["data"] == KANSHI
            root.chmod(0o755)
            with pytest.raises(RuntimeError, match="protected"):
                files.read("kanshi/config")
            root.chmod(0o770)
            with pytest.raises(RuntimeError, match="protected"):
                files.read("kanshi/config")
        assert (child / "config").read_bytes() == KANSHI


@pytest.mark.parametrize("authority", ["desktop-install.lock", "desktop-install.json"])
def test_private_outer_root_does_not_relax_writable_authority_file_check(tmp_path, authority):
    tmp_path.chmod(0o700)
    child = tmp_path / "panelbridge"
    child.mkdir()
    child.chmod(0o775)
    target = child / authority
    target.write_bytes(b"{}")
    target.chmod(0o660)
    with pytest.raises(RuntimeError, match="regular file"):
        UserSetup(tmp_path).install([])
    assert target.read_bytes() == b"{}"


def test_config_root_symlink_and_parent_traversal_are_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(RuntimeError):
        UserSetup(link).install([])
    with pytest.raises(RuntimeError):
        UserSetup(real / ".." / "real").install([])
    assert list(real.iterdir()) == []


def test_unconfirmed_directory_creation_is_safely_retained_after_interruption(tmp_path, monkeypatch):
    setup = UserSetup(tmp_path)
    save = setup._save

    def interrupted(record):
        if record["created_dirs"].get("labwc", {}).get("inode") is not None:
            raise OSError("directory ownership commit interrupted")
        save(record)

    monkeypatch.setattr(setup, "_save", interrupted)
    with pytest.raises(OSError, match="ownership commit"):
        setup.install([])
    assert (tmp_path / "labwc").is_dir()
    resumed = UserSetup(tmp_path)
    resumed.install([])
    resumed.uninstall()
    assert list((tmp_path / "labwc").iterdir()) == []
    assert not (tmp_path / "kanshi").exists()
    assert not (tmp_path / "autostart").exists()
