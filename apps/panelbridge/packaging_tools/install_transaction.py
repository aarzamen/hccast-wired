"""Root-only journal for five fixed PanelBridge configuration files.

This module neither installs a package nor starts services. A caller must supply
an explicit identity to replace/remove any preexisting file. The first backup
is retained permanently; changing an existing plan requires a future migration.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat


MAX_FILE_BYTES = 128 * 1024
MAX_JOURNAL_BYTES = 2 * 1024 * 1024
_TARGETS = {
    "enrollment": "etc/panelbridge/enrollment.json",
    "rescue_binding": "etc/panelbridge/rescue.json",
    "staged_helper_unit": "etc/systemd/system/panelbridge-helper.service",
    "staged_rescue_unit": "etc/systemd/system/panelbridge-rescue.service",
    "staged_bus_policy": "etc/dbus-1/system.d/org.panelbridge.Helper1.conf",
}
_JOURNAL = "var/lib/panelbridge/install-files.json"
_LOCK = "install-files.lock"
_CREATABLE = {"etc/panelbridge": 0o755, "var/lib/panelbridge": 0o700}
_PHASES = {"prepared", "applied", "restoring", "restored", "conflict"}
_HASH = re.compile(r"[0-9a-f]{64}\Z")


class TransactionError(RuntimeError):
    """No authority to mutate, invalid input, or interrupted filesystem I/O."""


class ConflictError(TransactionError):
    """Current bytes or metadata differ from both recorded states."""

    def __init__(self, targets):
        self.targets = tuple(sorted(targets))
        super().__init__("File conflict: " + ", ".join(self.targets))


@dataclass(frozen=True)
class FileIdentity:
    sha256: str
    mode: int
    uid: int
    gid: int


@dataclass(frozen=True)
class FileChange:
    data: bytes | None
    mode: int = 0o600
    gid: int | None = None
    expected: FileIdentity | None = None


def _integer(value, maximum=2**32 - 2):
    return type(value) is int and 0 <= value <= maximum


def _mode(value):
    return _integer(value, 0o777) and not value & 0o022


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _snapshot(data, mode, uid, gid):
    return {"data": base64.b64encode(data).decode("ascii"), "mode": mode,
            "uid": uid, "gid": gid, "sha256": _digest(data)}


def _identity(snapshot):
    if snapshot is None:
        return None
    return {key: snapshot[key] for key in ("sha256", "mode", "uid", "gid")}


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransactionError("Duplicate journal key")
        result[key] = value
    return result


class InstallFileTransaction:
    """Fixed production paths; all public calls require real and effective root.

    apply(changes) starts/repeats an identical plan; apply() resumes its intent.
    restore() reverses a plan using its first originals. status() reads its phase.
    None as FileChange.expected explicitly requires absence, including when the
    keyword is omitted. FileChange.data=None removes a recognized staging file.
    """

    def __init__(self):
        self._test = False
        self._base = Path("/")
        self._uid = self._gid = 0
        self._require_identity()

    @classmethod
    def _for_test(cls, root):
        """Private unprivileged fixture; never maps targets onto the host root."""
        if os.getuid() == 0 or os.geteuid() == 0:
            raise TransactionError("Test fixture requires an unprivileged process")
        base = Path(os.path.abspath(root))
        if base == Path("/"):
            raise TransactionError("Invalid test root")
        instance = object.__new__(cls)
        instance._test = True
        instance._base = base
        instance._uid, instance._gid = os.getuid(), os.getgid()
        with instance._base_fd():
            pass
        return instance

    def _require_identity(self):
        if self._test:
            if os.getuid() != self._uid or os.geteuid() != self._uid or self._uid == 0:
                raise TransactionError("Test process identity changed")
        elif os.getuid() != 0 or os.geteuid() != 0:
            raise TransactionError("Real and effective root are required")

    def _trusted_dir(self, info):
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != self._uid or info.st_mode & 0o022:
            raise TransactionError("Untrusted directory authority")

    @contextmanager
    def _base_fd(self):
        # No symlink is followed, including ancestors of the private test root.
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if not self._test:
                self._trusted_dir(os.fstat(fd))
            for part in self._base.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            self._trusted_dir(os.fstat(fd))
            yield fd
        except OSError as exc:
            raise TransactionError("Cannot open trusted filesystem root") from exc
        finally:
            os.close(fd)

    @contextmanager
    def _parent(self, base_fd, relative, *, create=False):
        fd = os.dup(base_fd)
        prefix = []
        try:
            for part in relative.split("/")[:-1]:
                prefix.append(part)
                path = "/".join(prefix)
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if path not in _CREATABLE:
                        raise TransactionError("Required system directory is absent") from None
                    if not create:
                        yield None
                        return
                    created = False
                    try:
                        os.mkdir(part, _CREATABLE[path], dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    try:
                        self._trusted_dir(os.fstat(next_fd))
                        if created:
                            os.fchmod(next_fd, _CREATABLE[path])
                            os.fsync(next_fd)
                        os.fsync(fd)
                    except BaseException:
                        os.close(next_fd)
                        raise
                try:
                    self._trusted_dir(os.fstat(next_fd))
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def _trusted_file(self, info, limit, *, private=False):
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != self._uid
                or not _mode(stat.S_IMODE(info.st_mode)) or info.st_size > limit
                or (private and stat.S_IMODE(info.st_mode) != 0o600)):
            raise TransactionError("Untrusted file authority")

    def _read(self, parent, name, limit, *, private=False):
        if parent is None:
            return None
        try:
            before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            return None
        self._trusted_file(before, limit, private=private)
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            opened = os.fstat(fd)
            self._trusted_file(opened, limit, private=private)
            if _signature(before) != _signature(opened):
                raise TransactionError("File changed while opening")
            chunks, length = [], 0
            while True:
                chunk = os.read(fd, min(65536, limit + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                if length > limit:
                    raise TransactionError("File exceeds size bound")
            if (_signature(opened) != _signature(os.fstat(fd))
                    or _signature(opened) != _signature(os.stat(name, dir_fd=parent, follow_symlinks=False))):
                raise TransactionError("File changed while reading")
            return _snapshot(b"".join(chunks), stat.S_IMODE(opened.st_mode), opened.st_uid, opened.st_gid)
        finally:
            os.close(fd)

    @contextmanager
    def _transaction(self, *, create):
        self._require_identity()
        try:
            with self._base_fd() as base_fd, self._parent(base_fd, _JOURNAL, create=create) as parent:
                if parent is None:
                    yield None
                    return
                lock_state = self._read(parent, _LOCK, 0, private=True)
                if lock_state is None and not create:
                    if self._read(parent, "install-files.json", MAX_JOURNAL_BYTES, private=True) is not None:
                        raise TransactionError("Journal has no transaction lock")
                    yield None
                    return
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                if lock_state is None:
                    flags |= os.O_CREAT | os.O_EXCL
                lock_fd = os.open(_LOCK, flags, 0o600, dir_fd=parent)
                try:
                    if lock_state is None:
                        os.fchmod(lock_fd, 0o600)
                    info = os.fstat(lock_fd)
                    self._trusted_file(info, 0, private=True)
                    if _signature(info) != _signature(os.stat(_LOCK, dir_fd=parent, follow_symlinks=False)):
                        raise TransactionError("Transaction lock changed")
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise TransactionError("Another file transaction holds the lock") from None
                    if lock_state is None:
                        os.fsync(lock_fd)
                    os.fsync(parent)
                    # Settle creation of the permitted journal directory too.
                    with self._parent(base_fd, "var/lib/placeholder") as grandparent:
                        os.fsync(grandparent)
                    yield (base_fd, parent)
                finally:
                    os.close(lock_fd)
        except OSError as exc:
            raise TransactionError("File transaction I/O failed") from exc

    def _write(self, parent, name, desired, expected, limit, *, private=False):
        current = self._read(parent, name, limit, private=private)
        if current != expected:
            raise ConflictError([name])
        if current == desired:
            os.fsync(parent)
            return
        if desired is None:
            # Deletion is limited to the fixed target supplied by the caller.
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
            return
        temporary = ".panelbridge-" + secrets.token_hex(16) + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        created = os.fstat(fd)
        try:
            if (created.st_uid, created.st_gid) != (desired["uid"], desired["gid"]):
                os.fchown(fd, desired["uid"], desired["gid"])
            os.fchmod(fd, desired["mode"])
            data = memoryview(base64.b64decode(desired["data"], validate=True))
            while data:
                count = os.write(fd, data)
                if count <= 0:
                    raise TransactionError("Incomplete file write")
                data = data[count:]
            os.fsync(fd)
            if self._read(parent, name, limit, private=private) != expected:
                raise ConflictError([name])
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(fd)
            try:
                remaining = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                remaining = None
            if remaining is not None and (remaining.st_dev, remaining.st_ino) == (created.st_dev, created.st_ino):
                os.unlink(temporary, dir_fd=parent)
                os.fsync(parent)

    def _validate_identity(self, identity):
        if (type(identity) is not dict or set(identity) != {"sha256", "mode", "uid", "gid"}
                or type(identity["sha256"]) is not str or not _HASH.fullmatch(identity["sha256"])
                or not _mode(identity["mode"]) or type(identity["uid"]) is not int
                or identity["uid"] != self._uid or not _integer(identity["gid"])):
            raise TransactionError("Invalid file identity")

    def _validate_snapshot(self, snapshot):
        if snapshot is None:
            return
        if type(snapshot) is not dict or set(snapshot) != {"data", "mode", "uid", "gid", "sha256"}:
            raise TransactionError("Invalid journal file snapshot")
        self._validate_identity(_identity(snapshot))
        try:
            if type(snapshot["data"]) is not str or len(snapshot["data"]) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                raise ValueError
            data = base64.b64decode(snapshot["data"], validate=True)
            if (len(data) > MAX_FILE_BYTES or base64.b64encode(data).decode("ascii") != snapshot["data"]
                    or _digest(data) != snapshot["sha256"]):
                raise ValueError
        except (ValueError, UnicodeError) as exc:
            raise TransactionError("Invalid journal snapshot bytes or hash") from exc

    def _validate_record(self, record):
        if (type(record) is not dict
                or set(record) != {"schema_version", "phase", "intent", "targets", "conflicts",
                                   "config_parent_was_absent"}
                or type(record["schema_version"]) is not int or record["schema_version"] != 1
                or type(record["phase"]) is not str or record["phase"] not in _PHASES
                or record["intent"] not in ("apply", "restore")
                or type(record["config_parent_was_absent"]) is not bool
                or type(record["targets"]) is not dict or not record["targets"]
                or not set(record["targets"]) <= _TARGETS.keys()):
            raise TransactionError("Invalid file transaction journal")
        if ((record["phase"] in ("prepared", "applied") and record["intent"] != "apply")
                or (record["phase"] in ("restoring", "restored") and record["intent"] != "restore")):
            raise TransactionError("Inconsistent journal intent")
        conflicts = record["conflicts"]
        if (type(conflicts) is not list or any(type(x) is not str for x in conflicts)
                or conflicts != sorted(set(conflicts)) or not set(conflicts) <= record["targets"].keys()
                or bool(conflicts) != (record["phase"] == "conflict")):
            raise TransactionError("Invalid journal conflicts")
        for entry in record["targets"].values():
            if type(entry) is not dict or set(entry) != {"before", "applied", "expected_before"}:
                raise TransactionError("Invalid journal target")
            self._validate_snapshot(entry["before"])
            self._validate_snapshot(entry["applied"])
            if entry["expected_before"] is not None:
                self._validate_identity(entry["expected_before"])
            if _identity(entry["before"]) != entry["expected_before"]:
                raise TransactionError("Original snapshot differs from explicit expectation")
        if record["config_parent_was_absent"]:
            config_entries = [entry for target, entry in record["targets"].items()
                              if target in ("enrollment", "rescue_binding")]
            if not config_entries or any(entry["before"] is not None for entry in config_entries):
                raise TransactionError("Invalid original configuration directory state")
        return record

    def _load(self, parent):
        snapshot = self._read(parent, "install-files.json", MAX_JOURNAL_BYTES, private=True)
        if snapshot is None:
            return None, None
        try:
            record = json.loads(base64.b64decode(snapshot["data"]), object_pairs_hook=_unique_object)
            return self._validate_record(record), snapshot
        except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
            raise TransactionError("Invalid file transaction journal") from exc

    def _save(self, parent, record, expected):
        self._validate_record(record)
        data = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
        if len(data) > MAX_JOURNAL_BYTES:
            raise TransactionError("Journal exceeds size bound")
        desired = _snapshot(data, 0o600, self._uid, self._gid)
        self._write(parent, "install-files.json", desired, expected, MAX_JOURNAL_BYTES, private=True)
        return desired

    def _changes(self, changes):
        if type(changes) is not dict or not changes or not set(changes) <= _TARGETS.keys():
            raise TransactionError("Only fixed target IDs are permitted")
        normalized = {}
        for target, change in changes.items():
            if (type(change) is not FileChange or not _mode(change.mode)
                    or (change.data is not None and (type(change.data) is not bytes or len(change.data) > MAX_FILE_BYTES))
                    or (change.gid is not None and not _integer(change.gid))):
                raise TransactionError("Invalid file change")
            expected = None
            if change.expected is not None:
                if type(change.expected) is not FileIdentity:
                    raise TransactionError("Expected identity must be explicit")
                expected = dict(vars(change.expected))
                self._validate_identity(expected)
            applied = None if change.data is None else _snapshot(
                change.data, change.mode, self._uid, self._gid if change.gid is None else change.gid)
            normalized[target] = {"expected_before": expected, "applied": applied}
        return normalized

    def _states(self, base_fd, record):
        states = {}
        for target in sorted(record["targets"]):
            relative = _TARGETS[target]
            with self._parent(base_fd, relative) as parent:
                states[target] = self._read(parent, relative.split("/")[-1], MAX_FILE_BYTES)
        return states

    @staticmethod
    def _summary(record):
        if record is None:
            return {"schema_version": 1, "phase": "absent", "intent": None, "targets": [], "conflicts": []}
        return {"schema_version": 1, "phase": record["phase"], "intent": record["intent"],
                "targets": sorted(record["targets"]), "conflicts": list(record["conflicts"])}

    def _run(self, base_fd, parent, record, journal_state, intent):
        states = self._states(base_fd, record)
        conflicts = [target for target, state in states.items()
                     if state not in (record["targets"][target]["before"], record["targets"][target]["applied"])]
        record["intent"] = intent
        record["conflicts"] = conflicts
        record["phase"] = "conflict" if conflicts else ("prepared" if intent == "apply" else "restoring")
        journal_state = self._save(parent, record, journal_state)
        if conflicts:
            raise ConflictError(conflicts)
        if intent == "apply" and record["config_parent_was_absent"]:
            with self._parent(base_fd, _TARGETS["enrollment"]) as config_parent:
                if config_parent is not None and stat.S_IMODE(os.fstat(config_parent).st_mode) != 0o755:
                    raise TransactionError("Incomplete configuration directory initialization; explicit repair required")
        desired_key = "applied" if intent == "apply" else "before"
        for target, current in states.items():
            relative = _TARGETS[target]
            desired = record["targets"][target][desired_key]
            try:
                with self._parent(base_fd, relative, create=desired is not None) as target_parent:
                    if target_parent is not None:
                        self._write(target_parent, relative.split("/")[-1], desired, current, MAX_FILE_BYTES)
                if relative.startswith("etc/panelbridge/"):
                    with self._parent(base_fd, "etc/placeholder") as grandparent:
                        os.fsync(grandparent)
            except ConflictError:
                record["phase"], record["conflicts"] = "conflict", [target]
                self._save(parent, record, journal_state)
                raise ConflictError([target]) from None
        record["phase"] = "applied" if intent == "apply" else "restored"
        self._save(parent, record, journal_state)
        return self._summary(record)

    def apply(self, changes=None):
        self._require_identity()
        normalized = None if changes is None else self._changes(changes)
        with self._transaction(create=True) as (base_fd, parent):
            record, journal_state = self._load(parent)
            if record is None:
                if normalized is None:
                    raise TransactionError("No saved transaction to resume")
                record = {"schema_version": 1, "phase": "prepared", "intent": "apply",
                          "targets": normalized, "conflicts": [], "config_parent_was_absent": False}
                if set(normalized) & {"enrollment", "rescue_binding"}:
                    with self._parent(base_fd, _TARGETS["enrollment"]) as config_parent:
                        record["config_parent_was_absent"] = config_parent is None
                states = self._states(base_fd, record)
                conflicts = [target for target, state in states.items()
                             if _identity(state) != normalized[target]["expected_before"]]
                if conflicts:
                    raise ConflictError(conflicts)
                for target, state in states.items():
                    record["targets"][target]["before"] = state
            else:
                if normalized is not None:
                    saved = {target: {key: entry[key] for key in ("expected_before", "applied")}
                             for target, entry in record["targets"].items()}
                    if normalized != saved:
                        raise TransactionError("Changed plans require an explicit upgrade migration")
                if record["intent"] == "restore" and record["phase"] != "restored":
                    raise TransactionError("Complete restoration before applying again")
            return self._run(base_fd, parent, record, journal_state, "apply")

    def restore(self):
        with self._transaction(create=False) as context:
            if context is None:
                return self._summary(None)
            base_fd, parent = context
            record, journal_state = self._load(parent)
            if record is None:
                return self._summary(None)
            return self._run(base_fd, parent, record, journal_state, "restore")

    def status(self):
        with self._transaction(create=False) as context:
            if context is None:
                return self._summary(None)
            record, _ = self._load(context[1])
            result = self._summary(record)
            if record is not None:
                states = self._states(context[0], record)
                result["current_conflicts"] = [target for target, state in states.items()
                    if state not in (record["targets"][target]["before"], record["targets"][target]["applied"])]
            return result
