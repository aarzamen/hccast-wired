"""Fixed-file root transaction semantics in a private, unprivileged test tree."""

import hashlib
import json
import os
from pathlib import Path
import stat
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools import install_transaction as module
from packaging_tools.install_transaction import (
    ConflictError, FileChange, FileIdentity, InstallFileTransaction, TransactionError,
)

PATHS = {
    "enrollment": "etc/panelbridge/enrollment.json",
    "rescue_binding": "etc/panelbridge/rescue.json",
    "staged_helper_unit": "etc/systemd/system/panelbridge-helper.service",
    "staged_rescue_unit": "etc/systemd/system/panelbridge-rescue.service",
    "staged_bus_policy": "etc/dbus-1/system.d/org.panelbridge.Helper1.conf",
}
JOURNAL = "var/lib/panelbridge/install-files.json"
BASELINE_DIRS = ("etc/panelbridge", "etc/systemd/system", "etc/dbus-1/system.d", "var/lib")


def trusted_directories(root, names=BASELINE_DIRS):
    """Give synthetic trusted ancestors explicit modes, independent of umask."""
    root.mkdir(exist_ok=True)
    root.chmod(0o755)
    for name in names:
        directory = root
        for part in Path(name).parts:
            directory /= part
            directory.mkdir(exist_ok=True)
            directory.chmod(0o755)
    return root


@pytest.fixture
def root(tmp_path):
    return trusted_directories(tmp_path / "root")


def transaction(root):
    return InstallFileTransaction._for_test(root)


def identity(path):
    info = path.stat()
    return FileIdentity(hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(info.st_mode),
                        info.st_uid, info.st_gid)


def existing(root, target, data=b"original\n", mode=0o640):
    path = root / PATHS[target]
    path.write_bytes(data)
    path.chmod(mode)
    return identity(path)


def plan(root):
    known = existing(root, "staged_helper_unit")
    return {"enrollment": FileChange(b'{"synthetic":true}\n'),
            "rescue_binding": FileChange(b"synthetic binding\n", mode=0o640),
            "staged_helper_unit": FileChange(None, expected=known)}


def assert_applied(root):
    assert (root / PATHS["enrollment"]).read_bytes() == b'{"synthetic":true}\n'
    assert (root / PATHS["rescue_binding"]).read_bytes() == b"synthetic binding\n"
    assert (root / PATHS["rescue_binding"]).stat().st_mode & 0o777 == 0o640
    assert not (root / PATHS["staged_helper_unit"]).exists()


def assert_restored(root):
    assert not (root / PATHS["enrollment"]).exists()
    assert not (root / PATHS["rescue_binding"]).exists()
    assert (root / PATHS["staged_helper_unit"]).read_bytes() == b"original\n"
    assert (root / PATHS["staged_helper_unit"]).stat().st_mode & 0o777 == 0o640


def test_fixed_target_apply_restore_retains_first_bytes_metadata_and_receipt(root):
    changes = plan(root)
    tx = transaction(root)
    assert tx.apply(changes)["phase"] == "applied"
    assert_applied(root)
    first = json.loads((root / JOURNAL).read_bytes())
    tx.apply(changes)
    assert json.loads((root / JOURNAL).read_bytes())["targets"] == first["targets"]
    assert tx.restore()["phase"] == "restored"
    assert_restored(root)
    tx.restore()
    assert_restored(root)
    tx.apply(changes)
    assert_applied(root)
    tx.restore()
    assert json.loads((root / JOURNAL).read_bytes())["targets"] == first["targets"]
    assert (root / JOURNAL).stat().st_mode & 0o777 == 0o600
    assert (root / "etc/panelbridge").is_dir()
    assert (root / "var/lib/panelbridge").is_dir()


@pytest.mark.parametrize("expectation", ["absent", "wrong_hash", "wrong_mode", "wrong_gid"])
def test_existing_unknown_or_mismatched_enrollment_is_never_adopted(root, expectation):
    known = existing(root, "enrollment", b"admin-owned enrollment", 0o600)
    if expectation == "absent":
        expected = None
    else:
        values = dict(sha256=known.sha256, mode=known.mode, uid=known.uid, gid=known.gid)
        values[{"wrong_hash": "sha256", "wrong_mode": "mode", "wrong_gid": "gid"}[expectation]] = {
            "wrong_hash": "0" * 64, "wrong_mode": 0o640, "wrong_gid": known.gid + 1}[expectation]
        expected = FileIdentity(**values)
    with pytest.raises(ConflictError):
        transaction(root).apply({"enrollment": FileChange(b"new", expected=expected)})
    assert (root / PATHS["enrollment"]).read_bytes() == b"admin-owned enrollment"
    assert not (root / JOURNAL).exists()


def test_explicit_matching_existing_bytes_can_be_replaced_and_exactly_restored(root):
    known = existing(root, "enrollment", b"first original", 0o640)
    tx = transaction(root)
    tx.apply({"enrollment": FileChange(b"new", mode=0o600, expected=known)})
    assert (root / PATHS["enrollment"]).read_bytes() == b"new"
    tx.restore()
    assert identity(root / PATHS["enrollment"]) == known


class Crash(RuntimeError):
    pass


@pytest.mark.parametrize("operation", ["replace", "fsync", "unlink"])
@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("action", ["apply", "restore"])
def test_failure_at_every_observed_mutation_boundary_resumes_without_losing_originals(
        root, tmp_path, monkeypatch, operation, after, action):
    # Discover the actual number of boundaries once; then replay each boundary
    # against a fresh synthetic filesystem, independent of internal step order.
    changes = plan(root)
    tx = transaction(root)
    if action == "restore":
        tx.apply(changes)
    original = getattr(module.os, operation)
    count = 0

    def counted(*args, **kwargs):
        nonlocal count
        count += 1
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, operation, counted)
        tx.apply(changes) if action == "apply" else tx.restore()
    assert count > 0
    for boundary in range(1, count + 1):
        case = trusted_directories(tmp_path / f"case-{boundary}")
        changes = plan(case)
        tx = transaction(case)
        if action == "restore":
            tx.apply(changes)
        calls = 0

        def failed(*args, **kwargs):
            nonlocal calls
            calls += 1
            hit = calls == boundary
            if hit and not after:
                raise Crash("before boundary")
            result = original(*args, **kwargs)
            if hit and after:
                raise Crash("after boundary")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(module.os, operation, failed)
            with pytest.raises(Crash):
                tx.apply(changes) if action == "apply" else tx.restore()
        resumed = transaction(case)
        if action == "apply":
            resumed.apply(changes)
            assert_applied(case)
        resumed.restore()
        assert_restored(case)


def test_all_target_conflicts_are_found_before_any_restore_and_admin_bytes_survive(root):
    tx = transaction(root)
    tx.apply(plan(root))
    (root / PATHS["rescue_binding"]).write_bytes(b"admin edit")
    with pytest.raises(ConflictError):
        tx.restore()
    assert (root / PATHS["enrollment"]).read_bytes() == b'{"synthetic":true}\n'
    assert (root / PATHS["rescue_binding"]).read_bytes() == b"admin edit"
    journal = json.loads((root / JOURNAL).read_bytes())
    assert journal["phase"] == "conflict" and journal["intent"] == "restore"
    assert journal["conflicts"] == ["rescue_binding"]
    with pytest.raises(TransactionError, match="restor"):
        tx.apply()
    (root / PATHS["rescue_binding"]).write_bytes(b"synthetic binding\n")
    tx.restore()
    assert_restored(root)


def test_changed_applied_plan_is_rejected_even_after_restore(root):
    tx = transaction(root)
    original = {"enrollment": FileChange(b"first")}
    tx.apply(original)
    tx.restore()
    before = (root / JOURNAL).read_bytes()
    with pytest.raises(TransactionError, match="upgrade|migration"):
        tx.apply({"enrollment": FileChange(b"second")})
    assert (root / JOURNAL).read_bytes() == before
    assert not (root / PATHS["enrollment"]).exists()


@pytest.mark.parametrize("target", [*PATHS, "journal", "lock"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "other_writer"])
def test_unsafe_file_authority_is_refused_without_touching_unrelated_bytes(root, target, kind):
    relative = PATHS.get(target, JOURNAL if target == "journal" else "var/lib/panelbridge/install-files.lock")
    path = root / relative
    trusted_directories(root, (str(Path(relative).parent),))
    outside = root / "untouched"
    outside.write_bytes(b"outside")
    outside.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, path)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_bytes(b"unsafe")
        path.chmod(0o620)
    changes = {target: FileChange(b"new")} if target in PATHS else {"enrollment": FileChange(b"new")}
    with pytest.raises(TransactionError):
        transaction(root).apply(changes)
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize("relative", ["etc", "etc/panelbridge", "etc/systemd/system", "var/lib"])
def test_symlink_or_other_writable_ancestor_has_no_authority(root, relative):
    parent = root / relative
    parent.chmod(0o777)
    with pytest.raises(TransactionError):
        transaction(root).apply({"staged_helper_unit": FileChange(b"unit"),
                                 "enrollment": FileChange(b"data")})
    parent.chmod(0o755)
    moved = parent.with_name(parent.name + "-moved")
    parent.rename(moved)
    parent.symlink_to(moved, target_is_directory=True)
    with pytest.raises(TransactionError):
        transaction(root).apply({"staged_helper_unit": FileChange(b"unit"),
                                 "enrollment": FileChange(b"data")})


@pytest.mark.parametrize("damage", ["path", "hash", "before", "mode", "phase", "extra", "duplicate", "oversized"])
def test_malformed_or_tampered_journal_is_never_replayed(root, damage):
    tx = transaction(root)
    tx.apply(plan(root))
    path = root / JOURNAL
    record = json.loads(path.read_bytes())
    entry = record["targets"]["enrollment"]
    if damage == "path":
        record["targets"]["../outside"] = entry
    elif damage == "hash":
        entry["applied"]["sha256"] = "0" * 64
    elif damage == "before":
        entry["before"] = entry["applied"]
    elif damage == "mode":
        entry["applied"]["mode"] = 0o666
    elif damage == "phase":
        record["phase"] = "done"
    elif damage == "extra":
        record["command"] = "arbitrary command"
    raw = json.dumps(record).encode()
    if damage == "duplicate":
        raw = raw[:-1] + b',"schema_version":1}'
    if damage == "oversized":
        raw = b" " * (2 * 1024 * 1024 + 1)
    path.write_bytes(raw)
    with pytest.raises(TransactionError):
        transaction(root).restore()
    assert_applied(root)
    assert path.read_bytes() == raw


def test_only_two_permitted_leaf_directories_may_be_created(root, tmp_path):
    (root / "etc/panelbridge").rmdir()
    tx = transaction(root)
    tx.apply({"enrollment": FileChange(b"new")})
    tx.restore()
    assert (root / "etc/panelbridge").is_dir()
    assert (root / "var/lib/panelbridge").is_dir()
    fresh = trusted_directories(tmp_path / "fresh", ("etc/systemd", "var/lib"))
    with pytest.raises(TransactionError):
        transaction(fresh).apply({"staged_helper_unit": FileChange(b"unit")})
    assert not (fresh / "etc/systemd/system").exists()


def test_public_api_rejects_unprivileged_identity_and_arbitrary_paths(root, monkeypatch):
    with pytest.raises(TransactionError, match="root"):
        InstallFileTransaction()
    with pytest.raises(TypeError):
        InstallFileTransaction(root)
    with pytest.raises(TransactionError):
        transaction(root).apply({"/etc/passwd": FileChange(b"bad")})
    with pytest.raises(TransactionError):
        transaction(root).apply({"enrollment": FileChange(b"x" * (128 * 1024 + 1))})


def test_exclusive_lock_prevents_another_writer(root):
    tx = transaction(root)
    with tx._transaction(create=True):
        with pytest.raises(TransactionError, match="transaction|lock"):
            transaction(root).apply({"enrollment": FileChange(b"new")})
    assert not (root / PATHS["enrollment"]).exists()


def test_status_and_restore_without_receipt_are_read_only(root):
    assert transaction(root).status()["phase"] == "absent"
    assert transaction(root).restore()["phase"] == "absent"
    assert not (root / "var/lib/panelbridge").exists()


def test_new_config_directory_gets_explicit_mode_despite_private_installer_umask(root):
    (root / "etc/panelbridge").rmdir()
    previous = os.umask(0o077)
    try:
        transaction(root).apply({"rescue_binding": FileChange(b"binding", mode=0o640)})
    finally:
        os.umask(previous)
    assert stat.S_IMODE((root / "etc/panelbridge").stat().st_mode) == 0o755
    assert stat.S_IMODE((root / "var/lib/panelbridge").stat().st_mode) == 0o700


def test_preexisting_private_config_directory_is_not_chmodded(root):
    (root / "etc/panelbridge").chmod(0o700)
    tx = transaction(root)
    tx.apply({"enrollment": FileChange(b"new")})
    tx.restore()
    assert stat.S_IMODE((root / "etc/panelbridge").stat().st_mode) == 0o700


def test_interrupted_mkdir_before_mode_setup_fails_clearly_until_explicit_repair(root, monkeypatch):
    (root / "etc/panelbridge").rmdir()
    original = module.os.mkdir

    def interrupted(path, *args, **kwargs):
        original(path, *args, **kwargs)
        # The journal exists only at the second permitted mkdir (etc/panelbridge).
        if (root / JOURNAL).exists():
            raise Crash("after configuration directory mkdir")

    previous = os.umask(0o077)
    try:
        with monkeypatch.context() as patch:
            patch.setattr(module.os, "mkdir", interrupted)
            with pytest.raises(Crash):
                transaction(root).apply({"enrollment": FileChange(b"new")})
    finally:
        os.umask(previous)
    assert stat.S_IMODE((root / "etc/panelbridge").stat().st_mode) == 0o700
    with pytest.raises(TransactionError, match="directory.*initialization"):
        transaction(root).apply()
    assert not (root / PATHS["enrollment"]).exists()
    # A separately reviewed repair resolves the ambiguous directory; replay does
    # not broaden the permissions of an object merely because it already exists.
    (root / "etc/panelbridge").chmod(0o755)
    assert transaction(root).apply()["phase"] == "applied"


def test_interrupted_directory_mode_and_parent_flush_replay(root, monkeypatch):
    (root / "etc/panelbridge").rmdir()
    original = module.os.fchmod

    def interrupted(fd, mode):
        original(fd, mode)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and mode == 0o755:
            raise Crash("after directory mode, before parent flush")

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fchmod", interrupted)
        with pytest.raises(Crash):
            transaction(root).apply({"enrollment": FileChange(b"new")})
    calls = []
    real_fsync = module.os.fsync
    etc_inode = (root / "etc").stat().st_ino

    def flushed(fd):
        calls.append(os.fstat(fd).st_ino)
        return real_fsync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "fsync", flushed)
        transaction(root).apply()
    assert etc_inode in calls
    assert stat.S_IMODE((root / "etc/panelbridge").stat().st_mode) == 0o755


@pytest.mark.parametrize("real,effective", [(0, 1000), (1000, 0), (1000, 1000)])
def test_both_root_ids_are_required_before_production_filesystem_access(monkeypatch, real, effective):
    monkeypatch.setattr(module.os, "getuid", lambda: real)
    monkeypatch.setattr(module.os, "geteuid", lambda: effective)
    with pytest.raises(TransactionError, match="root"):
        InstallFileTransaction()


def test_every_public_operation_rechecks_process_identity(root, monkeypatch):
    tx = transaction(root)
    monkeypatch.setattr(module.os, "geteuid", lambda: tx._uid + 1)
    for action in (lambda: tx.apply({"enrollment": FileChange(b"new")}), tx.restore, tx.status):
        with pytest.raises(TransactionError, match="identity"):
            action()
    assert not (root / "var/lib/panelbridge").exists()


def test_all_maximum_sized_binary_originals_fit_bounded_journal_and_restore(root):
    before = bytes(range(256)) * 512
    after = before[::-1]
    changes = {target: FileChange(after, mode=0o600, expected=existing(root, target, before))
               for target in PATHS}
    tx = transaction(root)
    tx.apply(changes)
    assert (root / JOURNAL).stat().st_size <= 2 * 1024 * 1024
    for path in PATHS.values():
        assert (root / path).read_bytes() == after
    tx.restore()
    for path in PATHS.values():
        assert (root / path).read_bytes() == before
        assert stat.S_IMODE((root / path).stat().st_mode) == 0o640


def test_wrong_file_owner_is_rejected_before_reading_or_replacing(root, monkeypatch):
    existing(root, "enrollment")
    original = module.os.stat

    def different_owner(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if path == "enrollment.json":
            values = list(info)
            values[4] = info.st_uid + 1
            return os.stat_result(values)
        return info

    with monkeypatch.context() as patch:
        patch.setattr(module.os, "stat", different_owner)
        with pytest.raises(TransactionError, match="authority"):
            transaction(root).apply({"enrollment": FileChange(b"new")})
    assert (root / PATHS["enrollment"]).read_bytes() == b"original\n"
    assert not (root / JOURNAL).exists()
