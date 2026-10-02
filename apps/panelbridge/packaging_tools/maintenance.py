"""Fresh-install root enrollment lifecycle; no APT, session restart or rescue enrollment.

Only the installed isolated launcher is a production entry point. Private fixture
construction keeps all writes below an owned unprivileged temporary root.
"""

import base64
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import time

from .runtime_access import RuntimeAccessError, validate_runtime

from .install_transaction import (
    FileChange, InstallFileTransaction, TransactionError, _signature, _snapshot, _unique_object,
)

MAX_BYTES = 262144
MAX_STATE = 16384
_STATE = "var/lib/panelbridge/maintenance.json"
_LOCK = "maintenance.lock"
_HELPER = "panelbridge-helper.service"
_RESCUE = "panelbridge-rescue.service"
_ENV = {"HOME": "/root", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
_OPERATIONS = ("enrollment", "user_configure", "reload", "helper_enable", "helper_start",
               "user_restore", "helper_stop", "helper_disable", "enrollment_restore")
_PHASES = {"prepared", "configuring", "awaiting_desktop_restart", "rollback_pending",
           "rolled_back", "removing", "pending_desktop_transition", "remove_ready"}
_SHADOWS = ("etc/panelbridge/rescue.json", "etc/systemd/system/panelbridge-helper.service",
            "etc/systemd/system/panelbridge-rescue.service",
            "etc/systemd/system/panelbridge-helper.service.d",
            "etc/systemd/system/panelbridge-rescue.service.d",
            "etc/dbus-1/system.d/org.panelbridge.Helper1.conf")
_USER_FILES = ("maintain-user-launch.py", "packaging_tools/__init__.py",
               "packaging_tools/user_maintenance.py", "packaging_tools/user_setup.py",
               "packaging_tools/desktop_setup.py", "packaging_tools/runtime_access.py")


class MaintenanceError(RuntimeError):
    """Fixed nonprivate reason; configuration failures never authorize guessing."""


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, RecursionError, TransactionError):
        raise MaintenanceError("invalid_bounded_json") from None


def _run(args, *, env, timeout=8):
    """Bound stdout and child lifetime; kill only this explicitly created process group."""
    process = None
    completed = False
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)
        data = bytearray()
        with selectors.DefaultSelector() as poller:
            os.set_blocking(process.stdout.fileno(), False)
            poller.register(process.stdout, selectors.EVENT_READ)
            while poller.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MaintenanceError("command_timeout")
                for key, _ in poller.select(remaining):
                    try:
                        chunk = os.read(key.fd, min(65536, MAX_BYTES + 1 - len(data)))
                    except BlockingIOError:
                        continue
                    if chunk:
                        data.extend(chunk)
                        if len(data) > MAX_BYTES:
                            raise MaintenanceError("command_output_too_large")
                    else:
                        poller.unregister(key.fd)
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise MaintenanceError("command_timeout") from None
        output = data.decode("utf-8", errors="strict")
        completed = True
        return code, output
    except (OSError, UnicodeError):
        raise MaintenanceError("command_unavailable") from None
    finally:
        if process is not None:
            if not completed:
                # A descendant may retain stdout after the leader exits. The
                # command's failure still requires cleanup of its owned group.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdout is not None:
                process.stdout.close()


def _properties(raw, expected):
    if not isinstance(raw, str) or len(raw) > MAX_BYTES:
        raise MaintenanceError("invalid_properties")
    result = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator or key not in expected or key in result:
            raise MaintenanceError("invalid_properties")
        result[key] = value
    if set(result) != set(expected):
        raise MaintenanceError("incomplete_properties")
    return result


def _public(action, state, **extra):
    return {"api_version": 1, "action": action, "state": state,
            "scope": "configuration_bytes_only", "live_output_restored": None, **extra}


class Maintenance:
    def __init__(self):
        if os.getuid() != 0 or os.geteuid() != 0 or not sys.flags.isolated:
            raise MaintenanceError("installed_isolated_root_required")
        self.files = InstallFileTransaction()
        self.run = _run
        self.record = self.saved = self.parent = None

    @classmethod
    def _for_test(cls, root, run):
        """Unprivileged fixture only; the CLI never exposes this constructor."""
        instance = object.__new__(cls)
        try:
            instance.files = InstallFileTransaction._for_test(root)
        except TransactionError:
            raise MaintenanceError("invalid_fixture_root") from None
        instance.run = run
        instance.record = instance.saved = instance.parent = None
        return instance

    def _command(self, args, *, timeout=8, env=None):
        code, output = self.run(args, env=dict(_ENV if env is None else env), timeout=timeout)
        if (type(code) is not int or type(output) is not str or len(output.encode("utf-8")) > MAX_BYTES):
            raise MaintenanceError("invalid_command_result")
        if code:
            raise MaintenanceError("fixed_command_failed")
        return output

    def _optional(self, relative):
        """Inspect a fixed root-relative path without following any symlink."""
        with self.files._base_fd() as base:
            fd = os.dup(base)
            try:
                parts = relative.split("/")
                for part in parts[:-1]:
                    try:
                        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    except FileNotFoundError:
                        return None
                    os.close(fd)
                    fd = next_fd
                    self.files._trusted_dir(os.fstat(fd))
                try:
                    return os.stat(parts[-1], dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    return None
            finally:
                os.close(fd)

    def _shadows(self):
        if any(self._optional(path) is not None for path in _SHADOWS):
            raise MaintenanceError("unowned_staging_or_rescue_configuration")

    def _installed(self):
        for name in _USER_FILES:
            info = self._optional("usr/lib/panelbridge/" + name)
            if info is None:
                raise MaintenanceError("installed_user_launcher_unavailable")
            self.files._trusted_file(info, 524288)

    def _account(self, uid):
        if type(uid) is not int or not 1000 <= uid < 2**31:
            raise MaintenanceError("invalid_normal_account")
        try:
            account = pwd.getpwuid(uid)
            if (account.pw_uid != uid or type(account.pw_gid) is not int or not 0 < account.pw_gid < 2**31
                    or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,63}", account.pw_name)
                    or not isinstance(account.pw_dir, str) or not account.pw_dir.startswith("/")
                    or account.pw_dir == "/" or "\x00" in account.pw_dir
                    or any(part in (".", "..") for part in account.pw_dir.split("/"))):
                raise ValueError
            by_name = pwd.getpwnam(account.pw_name)
            if (by_name.pw_uid, by_name.pw_gid, by_name.pw_dir) != (uid, account.pw_gid, account.pw_dir):
                raise ValueError
            fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                parts = Path(account.pw_dir).parts[1:]
                for part in parts:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd)
                    fd = next_fd
                info = os.fstat(fd)
                owner = self.files._uid if self.files._test else uid
                if info.st_uid != owner or info.st_mode & 0o022:
                    raise ValueError
            finally:
                os.close(fd)
            return account
        except (KeyError, ValueError, TypeError, OSError):
            raise MaintenanceError("invalid_normal_account") from None

    def _sudo_account(self):
        uid, gid, name = (os.environ.get(key, "") for key in ("SUDO_UID", "SUDO_GID", "SUDO_USER"))
        if not re.fullmatch(r"[1-9][0-9]{0,9}", uid) or not re.fullmatch(r"[1-9][0-9]{0,9}", gid):
            raise MaintenanceError("sudo_identity_required")
        account = self._account(int(uid))
        if account.pw_gid != int(gid) or account.pw_name != name:
            raise MaintenanceError("sudo_identity_mismatch")
        return account

    @staticmethod
    def _fingerprint(account):
        data = json.dumps([account.pw_uid, account.pw_gid, account.pw_name, account.pw_dir], separators=(",", ":"))
        return hashlib.sha256(data.encode()).hexdigest()

    def _record_account(self):
        account = self._account(self.record["normal_uid"])
        if self._fingerprint(account) != self.record["account_fingerprint"]:
            raise MaintenanceError("recorded_account_changed")
        return account

    def _sessions(self, account):
        reply = _json(self._command(["/usr/bin/busctl", "--system", "--json=short", "call", "org.freedesktop.login1",
            "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListSessions"]))
        if (not isinstance(reply, dict) or set(reply) != {"type", "data"} or reply["type"] != "a(susso)"
                or not isinstance(reply["data"], list) or len(reply["data"]) != 1
                or not isinstance(reply["data"][0], list) or len(reply["data"][0]) > 64):
            raise MaintenanceError("invalid_logind_sessions")
        ids = []
        for entry in reply["data"][0]:
            if (not isinstance(entry, list) or len(entry) != 5 or type(entry[1]) is not int
                    or not all(isinstance(entry[index], str) for index in (0, 2, 3, 4))):
                raise MaintenanceError("invalid_logind_sessions")
            if entry[1] == account.pw_uid:
                if entry[2] != account.pw_name:
                    raise MaintenanceError("logind_identity_mismatch")
                ids.append(entry[0])
        if (len(ids) > 16 or len(set(ids)) != len(ids)
                or any(not re.fullmatch(r"[a-zA-Z0-9]{1,32}", value) for value in ids)):
            raise MaintenanceError("invalid_logind_sessions")
        properties = ("User", "Name", "Active", "Remote", "Type", "Class", "State", "LockedHint")
        sessions = []
        for session in ids:
            args = ["/usr/bin/loginctl", "show-session", session, "--no-pager"]
            args.extend("--property=" + key for key in properties)
            values = _properties(self._command(args, timeout=3), properties)
            if (values["User"] != str(account.pw_uid) or values["Name"] != account.pw_name
                    or any(values[key] not in ("yes", "no") for key in ("Active", "Remote", "LockedHint"))):
                raise MaintenanceError("logind_identity_mismatch")
            if values["Type"] in ("wayland", "x11") and values["Class"] == "user":
                sessions.append(values)
        return sessions

    def _runtime(self, account, required):
        relative = f"run/user/{account.pw_uid}"
        fd = None
        try:
            # /run and /run/user stay root authority; the final runtime is the
            # normal account's directory and is checked independently.
            with self.files._base_fd() as base:
                parent = os.open("run", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=base)
                try:
                    self.files._trusted_dir(os.fstat(parent))
                    users = os.open("user", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        self.files._trusted_dir(os.fstat(users))
                        fd = os.open(str(account.pw_uid), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=users)
                    finally:
                        os.close(users)
                finally:
                    os.close(parent)
            info = os.fstat(fd)
            owner = self.files._uid if self.files._test else account.pw_uid
            try:
                validate_runtime(fd, owner)
            except RuntimeAccessError:
                raise MaintenanceError("unsafe_user_runtime") from None
            names = []
            with os.scandir(fd) as entries:
                for entry in entries:
                    if len(names) == 256:
                        raise MaintenanceError("user_runtime_too_large")
                    names.append(entry.name)
            environment = {"XDG_RUNTIME_DIR": "/" + relative}
            sockets = []
            for name in names:
                if re.fullmatch(r"wayland-[0-9]{1,3}", name) or name == "bus":
                    endpoint = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != owner:
                        raise MaintenanceError("unsafe_user_socket")
                    if name == "bus":
                        environment["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/" + relative + "/bus"
                    else:
                        sockets.append(name)
            if required and len(sockets) != 1:
                raise MaintenanceError("one_wayland_socket_required")
            if len(sockets) == 1:
                environment.update(WAYLAND_DISPLAY=sockets[0], XDG_SESSION_TYPE="wayland")
            return environment
        except FileNotFoundError:
            if required:
                raise MaintenanceError("user_runtime_unavailable") from None
            return {}
        except TransactionError as error:
            # The shared anchored-root context wraps OSError from its body.
            # A logged-out account legitimately has no /run/user/<uid>.
            if isinstance(error.__cause__, FileNotFoundError):
                if not required:
                    return {}
                raise MaintenanceError("user_runtime_unavailable") from None
            raise MaintenanceError("unsafe_user_runtime") from None
        except OSError:
            raise MaintenanceError("unsafe_user_runtime") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _as_user(self, account, args, runtime):
        self._installed()
        environment = {"HOME": account.pw_dir, "USER": account.pw_name, "LOGNAME": account.pw_name,
                       "PATH": "/usr/bin:/bin", "LC_ALL": "C", **runtime}
        command = ["/usr/sbin/runuser", "--user", account.pw_name, "--", "/usr/bin/env", "-i"]
        command.extend(key + "=" + value for key, value in sorted(environment.items()))
        command.extend(args)
        if self._runtime(account, bool(runtime.get("WAYLAND_DISPLAY"))) != runtime:
            raise MaintenanceError("user_runtime_changed")
        return self.run(command, env=dict(_ENV), timeout=12)

    def _user(self, account, action, runtime):
        code, raw = self._as_user(account, ["/usr/bin/python3", "-I", "-B",
            "/usr/lib/panelbridge/maintain-user-launch.py", action], runtime)
        if not isinstance(raw, str) or len(raw.encode()) > MAX_STATE:
            raise MaintenanceError("invalid_user_receipt")
        result = _json(raw)
        if (not isinstance(result, dict) or type(result.get("api_version")) is not int or result["api_version"] != 1
                or result.get("action") != action or result.get("scope") != "configuration_bytes_only"
                or "live_output_restored" not in result or result["live_output_restored"] is not None):
            raise MaintenanceError("invalid_user_receipt")
        allowed = {"configure": {"configured"}, "restore": {"restored", "not_configured"},
                   "status": {"configured", "restored", "not_configured"}}[action]
        if type(code) is not int or code != 0 or not isinstance(result.get("state"), str) or result["state"] not in allowed:
            raise MaintenanceError("user_configuration_requires_reconciliation")
        return {"state": result["state"], "scope": "configuration_bytes_only", "live_output_restored": None}

    def _user_bus(self, account, args, runtime, expected):
        code, raw = self._as_user(account, ["/usr/bin/busctl", *args], runtime)
        if code or not isinstance(raw, str) or len(raw.encode()) > MAX_STATE:
            raise MaintenanceError("idle_query_failed")
        result = _json(raw)
        if (not isinstance(result, dict) or set(result) != {"type", "data"}
                or result["type"] != expected or not isinstance(result["data"], list) or len(result["data"]) != 1):
            raise MaintenanceError("invalid_idle_query")
        return result["data"][0]

    def _service(self, unit):
        fields = ("LoadState", "ActiveState", "SubState", "UnitFileState", "FragmentPath", "DropInPaths")
        args = ["/usr/bin/systemctl", "show", "--no-pager"]
        args.extend("--property=" + key for key in fields)
        args.append(unit)
        values = _properties(self._command(args), fields)
        owned_failed = (unit == _HELPER and self.record is not None
                        and self.record["operations"]["helper_start"] != "not_started"
                        and values["ActiveState"] == values["SubState"] == "failed")
        if (values["LoadState"] != "loaded" or values["FragmentPath"] != "/usr/lib/systemd/system/" + unit
                or values["DropInPaths"] or (values["ActiveState"] not in ("active", "inactive") and not owned_failed)
                or (values["ActiveState"] == "inactive" and values["SubState"] != "dead")
                or values["UnitFileState"] not in (("static", "disabled") if unit == _RESCUE else ("enabled", "disabled"))):
            raise MaintenanceError("unsupported_service_state")
        result = {"active": values["ActiveState"] == "active", "enabled": values["UnitFileState"] == "enabled"}
        if owned_failed:
            result["failed"] = True
        return result

    def _network_restored(self):
        with self.files._base_fd() as base, self.files._parent(base, "var/lib/panelbridge/network-transaction.json") as parent:
            snapshot = self.files._read(parent, "network-transaction.json", MAX_BYTES)
        if snapshot is not None:
            result = _json(base64.b64decode(snapshot["data"]))
            if (not isinstance(result, dict) or type(result.get("api_version")) is not int
                    or result["api_version"] != 1 or result.get("state") != "restored"):
                raise MaintenanceError("network_restoration_unverified")

    def _processes_idle(self):
        raw = self._command(["/usr/bin/ps", "-ww", "-eo", "pid=,uid=,args="])
        if len(raw.splitlines()) > 8192:
            raise MaintenanceError("process_inventory_too_large")
        for line in raw.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise MaintenanceError("invalid_process_inventory")
            # ps arguments are display text, not shell syntax; unmatched quotes
            # in unrelated arguments cannot invalidate the whole inventory.
            words = fields[2].split()
            if (any(word in ("/usr/lib/panelbridge/session-launch", "/usr/lib/panelbridge/session-launch.py",
                             "/usr/lib/panelbridge/rescue-launch.py", "/usr/lib/panelbridge/bin/panelbridge-wfd-worker",
                             "panelbridge.session_service", "panelbridge.rescue_service") for word in words)
                    or (self.record and fields[1] == str(self.record["normal_uid"])
                        and words and Path(words[0]).name == "wf-recorder")):
                raise MaintenanceError("owned_session_or_media_active")

    def _idle(self, account=None, runtime=None, graphical=False):
        if self._service(_RESCUE)["active"]:
            raise MaintenanceError("rescue_active")
        self._processes_idle()
        if account is not None:
            if runtime and "DBUS_SESSION_BUS_ADDRESS" in runtime:
                present = self._user_bus(account, ["--user", "--json=short", "call", "org.freedesktop.DBus",
                    "/org/freedesktop/DBus", "org.freedesktop.DBus", "NameHasOwner", "s", "org.panelbridge.Session1"], runtime, "b")
                if type(present) is not bool or present:
                    raise MaintenanceError("session_bus_owner_active_or_unknown")
            elif graphical:
                raise MaintenanceError("graphical_user_bus_unavailable")
            if self._service(_HELPER)["active"]:
                raw = self._user_bus(account, ["--system", "--json=short", "call", "org.panelbridge.Helper1",
                    "/org/panelbridge/Helper1", "org.panelbridge.Helper1", "Status"], runtime or {}, "s")
                value = _json(raw)
                if (not isinstance(value, dict) or value.get("api_version") != 1 or value.get("state") != "idle"
                        or value.get("owner_present") is not False):
                    raise MaintenanceError("helper_not_idle")
        self._network_restored()

    @contextmanager
    def _locked(self, create):
        try:
            with self._locked_files(create):
                yield
        except (TransactionError, OSError):
            raise MaintenanceError("trusted_transaction_failed") from None

    @contextmanager
    def _locked_files(self, create):
        self.files._require_identity()
        with self.files._base_fd() as base, self.files._parent(base, _STATE, create=create) as parent:
            self.parent = parent
            self.record = self.saved = None
            if parent is None:
                yield
                return
            if stat.S_IMODE(os.fstat(parent).st_mode) != 0o700:
                raise MaintenanceError("lifecycle_directory_not_private")
            lock_state = self.files._read(parent, _LOCK, 0, private=True)
            if lock_state is None and not create:
                if self.files._read(parent, "maintenance.json", MAX_STATE, private=True) is not None:
                    raise MaintenanceError("lifecycle_lock_missing")
                yield
                return
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
            if lock_state is None:
                flags |= os.O_CREAT | os.O_EXCL
            lock = os.open(_LOCK, flags, 0o600, dir_fd=parent)
            try:
                info = os.fstat(lock)
                self.files._trusted_file(info, 0, private=True)
                if _signature(info) != _signature(os.stat(_LOCK, dir_fd=parent, follow_symlinks=False)):
                    raise MaintenanceError("lifecycle_lock_changed")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise MaintenanceError("maintenance_busy") from None
                if lock_state is None:
                    os.fsync(lock)
                    os.fsync(parent)
                self.saved = self.files._read(parent, "maintenance.json", MAX_STATE, private=True)
                if self.saved is not None:
                    self.record = _json(base64.b64decode(self.saved["data"]))
                    self._validate()
                yield
            finally:
                os.close(lock)
                self.parent = None

    def _validate(self):
        value = self.record
        if (type(value) is not dict or set(value) != {"schema_version", "normal_uid", "account_fingerprint", "phase",
                "initial_helper", "operations", "user_setup", "user_restore"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1
                or type(value["normal_uid"]) is not int or not 1000 <= value["normal_uid"] < 2**31
                or not isinstance(value["account_fingerprint"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["account_fingerprint"])
                or not isinstance(value["phase"], str) or value["phase"] not in _PHASES
                or value["initial_helper"] != {"active": False, "enabled": False}
                or any(type(item) is not bool for item in value["initial_helper"].values())
                or not isinstance(value["operations"], dict) or set(value["operations"]) != set(_OPERATIONS)
                or any(state not in ("not_started", "pending", "done") for state in value["operations"].values())):
            raise MaintenanceError("invalid_lifecycle_journal")
        for name, states in (("user_setup", {"configured"}), ("user_restore", {"restored", "not_configured"})):
            receipt = value[name]
            if receipt is not None and (not isinstance(receipt, dict) or set(receipt) != {"state", "scope", "live_output_restored"}
                    or not isinstance(receipt["state"], str) or receipt["state"] not in states or receipt["scope"] != "configuration_bytes_only"
                    or receipt["live_output_restored"] is not None):
                raise MaintenanceError("invalid_lifecycle_user_receipt")
        done = value["operations"]
        if value["phase"] == "awaiting_desktop_restart" and (
                any(done[name] != "done" for name in _OPERATIONS[:5]) or value["user_setup"] is None):
            raise MaintenanceError("incomplete_configured_journal")
        if value["phase"] in ("pending_desktop_transition", "remove_ready") and (
                done["user_restore"] != "done" or value["user_restore"] is None):
            raise MaintenanceError("incomplete_restoration_journal")
        if value["phase"] == "remove_ready" and any(done[name] != "done" for name in _OPERATIONS[5:]):
            raise MaintenanceError("incomplete_removed_journal")

    def _save(self):
        self._validate()
        raw = (json.dumps(self.record, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
        if len(raw) > MAX_STATE:
            raise MaintenanceError("lifecycle_journal_too_large")
        desired = _snapshot(raw, 0o600, self.files._uid, self.files._gid)
        self.files._write(self.parent, "maintenance.json", desired, self.saved, MAX_STATE, private=True)
        self.saved = desired

    def _step(self, name, operation):
        self.record["operations"][name] = "pending"
        self._save()
        result = operation()
        if name == "user_configure":
            self.record["user_setup"] = result
        elif name == "user_restore":
            self.record["user_restore"] = result
        self.record["operations"][name] = "done"
        self._save()
        return result

    def _plan(self):
        raw = (json.dumps({"api_version": 1, "normal_uid": self.record["normal_uid"]}, sort_keys=True) + "\n").encode()
        return {"enrollment": FileChange(raw)}

    def _owned_enrollment(self):
        expected = self.files._changes(self._plan())
        with self.files._transaction(create=False) as context:
            if context is None:
                if self.record["operations"]["enrollment"] == "done":
                    raise MaintenanceError("enrollment_receipt_missing")
                return
            record, _ = self.files._load(context[1])
            if record is None:
                if self.record["operations"]["enrollment"] == "done":
                    raise MaintenanceError("enrollment_receipt_missing")
                return
            actual = {target: {key: entry[key] for key in ("expected_before", "applied")}
                      for target, entry in record["targets"].items()}
            if actual != expected or record["targets"]["enrollment"]["before"] is not None:
                raise MaintenanceError("enrollment_receipt_not_owned")
            states = self.files._states(context[0], record)
            if states["enrollment"] not in (None, record["targets"]["enrollment"]["applied"]):
                raise MaintenanceError("enrollment_conflict")
            if self.record["phase"] == "remove_ready" and states["enrollment"] is not None:
                raise MaintenanceError("enrollment_changed_after_removal")

    def _set_service(self, verb):
        state = self._service(_HELPER)
        key, desired = {"enable": ("enabled", True), "disable": ("enabled", False),
                        "start": ("active", True), "stop": ("active", False)}[verb]
        if state[key] != desired or (verb == "stop" and state.get("failed")):
            self._command(["/usr/bin/systemctl", verb, "--", _HELPER], timeout=50)
        if verb == "stop" and state.get("failed"):
            self._command(["/usr/bin/systemctl", "reset-failed", "--", _HELPER])
        after = self._service(_HELPER)
        if after[key] != desired or (verb == "stop" and after.get("failed")):
            raise MaintenanceError("helper_transition_unverified")

    def _reload(self):
        self._command(["/usr/bin/systemctl", "daemon-reload"])
        self._command(["/usr/bin/busctl", "--system", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
                       "org.freedesktop.DBus", "ReloadConfig"])

    def _fresh(self):
        self._shadows()
        if self._optional("etc/panelbridge/enrollment.json") is not None or self.files.status()["phase"] != "absent":
            raise MaintenanceError("existing_enrollment_or_transaction")
        if self._service(_HELPER) != {"enabled": False, "active": False}:
            raise MaintenanceError("unowned_helper_state")
        self._idle()

    def _guard_service_ownership(self):
        current = self._service(_HELPER)
        for key, operation in (("enabled", "helper_enable"), ("active", "helper_start")):
            if current[key] and self.record["operations"][operation] == "not_started":
                raise MaintenanceError("unowned_helper_state")

    def _rollback(self, account, runtime):
        self.record["phase"] = "rollback_pending"
        self._save()
        self._owned_enrollment()
        self._guard_service_ownership()
        self._idle(account, runtime, bool(self._sessions(account)))
        if self.record["operations"]["user_configure"] != "not_started":
            self._step("user_restore", lambda: self._user(account, "restore", runtime))
        if self.record["operations"]["helper_start"] != "not_started":
            self._step("helper_stop", lambda: self._set_service("stop"))
        self._network_restored()
        if self.record["operations"]["helper_enable"] != "not_started":
            self._step("helper_disable", lambda: self._set_service("disable"))
        if self.record["operations"]["enrollment"] != "not_started":
            self._step("enrollment_restore", self.files.restore)
        self.record["phase"] = "rolled_back"
        self._save()

    def configure(self):
        account = self._sudo_account()
        self._installed()
        sessions = self._sessions(account)
        if len(sessions) != 1 or any(sessions[0][key] != value for key, value in {
                "Type": "wayland", "Active": "yes", "Remote": "no", "LockedHint": "no", "State": "active"}.items()):
            raise MaintenanceError("active_local_wayland_session_required")
        runtime = self._runtime(account, True)
        self._shadows()
        with self._locked(False):
            if self.record is None:
                self._fresh()
        with self._locked(True):
            if self.record is None:
                self._fresh()
                self.record = {"schema_version": 1, "normal_uid": account.pw_uid,
                    "account_fingerprint": self._fingerprint(account), "phase": "prepared",
                    "initial_helper": {"enabled": False, "active": False},
                    "operations": dict.fromkeys(_OPERATIONS, "not_started"), "user_setup": None, "user_restore": None}
                self._save()
            if self.record["normal_uid"] != account.pw_uid or self._fingerprint(account) != self.record["account_fingerprint"]:
                raise MaintenanceError("recorded_account_changed")
            if self.record["phase"] in ("removing", "pending_desktop_transition", "remove_ready"):
                raise MaintenanceError("removal_in_progress_or_complete")
            if self.record["phase"] == "rollback_pending":
                self._rollback(account, runtime)
                raise MaintenanceError("previous_configuration_rolled_back")
            self._owned_enrollment()
            self._guard_service_ownership()
            self._idle(account, runtime, True)
            if self.record["phase"] == "rolled_back":
                self.record["operations"] = dict.fromkeys(_OPERATIONS, "not_started")
                self.record["user_setup"] = self.record["user_restore"] = None
            self.record["phase"] = "configuring"
            self._save()
            try:
                self._step("enrollment", lambda: self.files.apply(self._plan()))
                self._step("user_configure", lambda: self._user(account, "configure", runtime))
                self._step("reload", self._reload)
                self._step("helper_enable", lambda: self._set_service("enable"))
                self._step("helper_start", lambda: self._set_service("start"))
                self._idle(account, runtime, True)
                self.record["phase"] = "awaiting_desktop_restart"
                self._save()
            except (MaintenanceError, TransactionError, OSError):
                try:
                    self._rollback(account, runtime)
                except (MaintenanceError, TransactionError, OSError):
                    raise MaintenanceError("configuration_failed_rollback_pending") from None
                raise MaintenanceError("configuration_failed_rolled_back") from None
            return _public("configure", "awaiting_desktop_restart")

    def prepare_remove(self):
        with self._locked(False):
            if self.record is None:
                self._fresh()
                return _public("prepare-remove", "not_configured")
            self._shadows()
            self._owned_enrollment()
            account = self._record_account()
            self._guard_service_ownership()
            sessions = self._sessions(account)
            runtime = self._runtime(account, False)
            self._idle(account, runtime, bool(sessions))
            if self.record["phase"] == "remove_ready":
                if self._service(_HELPER) != {"enabled": False, "active": False}:
                    raise MaintenanceError("helper_state_changed_after_removal")
                return _public("prepare-remove", "remove_ready")
            self.record["phase"] = "removing"
            self._save()
            self._step("user_restore", lambda: self._user(account, "restore", runtime))
            if self._sessions(account):
                self.record["phase"] = "pending_desktop_transition"
                self._save()
                return _public("prepare-remove", "pending_desktop_transition")
            self._idle(account, runtime)
            self._step("helper_stop", lambda: self._set_service("stop"))
            self._network_restored()
            self._idle(account, runtime)
            self._step("helper_disable", lambda: self._set_service("disable"))
            self._step("enrollment_restore", self.files.restore)
            self.record["phase"] = "remove_ready"
            self._save()
            return _public("prepare-remove", "remove_ready")

    def status(self):
        with self._locked(False):
            return _public("status", self.record["phase"] if self.record else "not_configured")


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in ("configure", "prepare-remove", "status"):
        result, code = _public(None, "invalid_request", reason="expected_fixed_action"), 2
    else:
        action = arguments[0]
        try:
            lifecycle = Maintenance()
            result = {"configure": lifecycle.configure, "prepare-remove": lifecycle.prepare_remove,
                      "status": lifecycle.status}[action]()
            code = 4 if result["state"] == "pending_desktop_transition" else 0
        except MaintenanceError as error:
            authority = str(error) in ("installed_isolated_root_required", "sudo_identity_required", "sudo_identity_mismatch", "invalid_normal_account")
            result = _public(action, "authority_refused" if authority else "conflict", reason=str(error))
            code = 5 if authority else 3
        except (TransactionError, OSError):
            result, code = _public(action, "operation_failed", reason="trusted_transaction_failed"), 6
    print(json.dumps(result, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
