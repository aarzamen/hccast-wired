"""Root-owned system D-Bus entry point for PanelBridge's network helper.

Run through the installed isolated-mode launcher. The event loop only schedules
bounded work: all credential lookups, NM operations and teardown run in workers.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import threading
import time
import uuid

from .network import Budget, GioNetworkBackend, HelperError, NetworkHelper

NAME = "org.panelbridge.Helper1"
OBJECT = "/org/panelbridge/Helper1"
ENROLLMENT = Path("/etc/panelbridge/enrollment.json")
JOURNAL_DIR = Path("/var/lib/panelbridge")
RESCUE_UNIT = "panelbridge-rescue.service"
METHODS = {"Status", "Discover", "Connect", "KeepAlive", "Release"}
UNIQUE_SENDER = re.compile(r":[0-9]+\.[0-9]+\Z")
INTROSPECTION = """<node><interface name="org.panelbridge.Helper1">
<method name="Status"><arg type="s" name="result" direction="out"/></method>
<method name="Discover"><arg type="s" name="result" direction="out"/></method>
<method name="Connect"><arg type="s" name="candidate_id" direction="in"/><arg type="s" name="result" direction="out"/></method>
<method name="KeepAlive"><arg type="s" name="result" direction="out"/></method>
<method name="Release"><arg type="s" name="result" direction="out"/></method>
</interface></node>"""


@dataclass(frozen=True)
class Enrollment:
    normal_uid: int
    rescue_uid: int | None = None


def _trusted_directory(path, code):
    """Open every directory without following symlinks, reject user authority."""
    path = Path(path)
    if not path.is_absolute():
        raise HelperError(code, "Trusted state directory must be absolute")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise HelperError(code, "State directory must be root-owned and nonwritable by other users")
        return fd
    except (OSError, HelperError):
        os.close(fd)
        raise HelperError(code, "Trusted state directory is missing or unsafe") from None


def _read_json_at(directory_fd, name, code):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or info.st_nlink != 1:
            raise HelperError(code, "State file must be a root-owned regular file")
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise HelperError(code, "State file exceeds size limit")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise HelperError(code, "State file is malformed") from None


def load_enrollment(path=ENROLLMENT):
    code = "EnrollmentInvalid"
    try:
        directory_fd = _trusted_directory(Path(path).parent, code)
        try:
            data = _read_json_at(directory_fd, Path(path).name, code)
        finally:
            os.close(directory_fd)
        if not isinstance(data, dict) or set(data) - {"api_version", "normal_uid", "rescue_uid"}:
            raise ValueError
        normal, rescue = data["normal_uid"], data.get("rescue_uid")
        if (data.get("api_version") != 1 or type(normal) is not int or not 1000 <= normal < 2**31
                or (rescue is not None and (type(rescue) is not int or not 0 < rescue < 2**31 or rescue == normal))):
            raise ValueError
        return Enrollment(normal, rescue)
    except (OSError, ValueError, KeyError, TypeError):
        raise HelperError(code, "Enrollment is missing or invalid") from None


class FileJournal:
    """One durable app transaction; incomplete records survive every restart."""
    def __init__(self, directory=JOURNAL_DIR):
        self.directory = Path(directory)

    def read(self):
        code = "JournalInvalid"
        directory_fd = _trusted_directory(self.directory, code)
        try:
            try:
                data = _read_json_at(directory_fd, "network-transaction.json", code)
            except FileNotFoundError:
                return None
            if not isinstance(data, dict) or data.get("api_version") != 1 or not isinstance(data.get("state"), str):
                raise HelperError(code, "Network transaction is invalid")
            if data["state"] != "restored" and not isinstance(data.get("snapshot"), dict):
                raise HelperError(code, "Network transaction has no restoration baseline")
            return data
        except OSError:
            raise HelperError(code, "Network transaction could not be read") from None
        finally:
            os.close(directory_fd)

    def write(self, record):
        code = "JournalInvalid"
        raw = (json.dumps(record, allow_nan=False, sort_keys=True) + "\n").encode()
        if len(raw) > 65536:
            raise HelperError(code, "Network transaction exceeds size limit")
        directory_fd = _trusted_directory(self.directory, code)
        temporary = ".network-" + uuid.uuid4().hex + ".tmp"
        created = False
        try:
            # Do not silently replace an existing symlink, hardlink or foreign file.
            try:
                _read_json_at(directory_fd, "network-transaction.json", code)
            except FileNotFoundError:
                pass
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=directory_fd)
            created = True
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, "network-transaction.json", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            created = False
            os.fsync(directory_fd)
        except OSError:
            raise HelperError(code, "Network transaction could not be saved") from None
        finally:
            if created:
                os.unlink(temporary, dir_fd=directory_fd)
            os.close(directory_fd)


class Authority:
    def __init__(self, enrollment_loader, identities):
        self.enrollment_loader, self.identities = enrollment_loader, identities

    def authorize(self, sender, method):
        if method not in METHODS or not isinstance(sender, str) or not UNIQUE_SENDER.fullmatch(sender):
            raise HelperError("NotAuthorized", "Invalid helper caller or method")
        enrollment = self.enrollment_loader()
        uid, pid = self.identities.credentials(sender)
        if uid == enrollment.normal_uid:
            # Lock/logout must not prevent the enrolled account from restoring
            # its own lease; NetworkHelper still rejects another unique owner.
            if method in ("Status", "Release") or self.identities.active_graphical(uid):
                return uid
        elif enrollment.rescue_uid is not None and uid == enrollment.rescue_uid:
            if self.identities.rescue_service(pid, method):
                return uid
        raise HelperError("NotAuthorized", "An enrolled active local graphical session is required")


class GioIdentities:
    def __init__(self, bus, Gio, GLib):
        self.bus, self.Gio, self.GLib = bus, Gio, GLib

    def call(self, destination, path, interface, method, signature, args, budget):
        try:
            value = self.GLib.Variant(signature, args)
            return self.bus.call_sync(destination, path, interface, method, value, None,
                                      self.Gio.DBusCallFlags.NONE, budget.timeout_ms(2000), None).unpack()
        except Exception:
            raise HelperError("NotAuthorized", "Caller identity could not be verified") from None

    def props(self, destination, path, interface, budget):
        return self.call(destination, path, "org.freedesktop.DBus.Properties", "GetAll", "(s)", (interface,), budget)[0]

    def credentials(self, sender):
        values = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                           "GetConnectionCredentials", "(s)", (sender,), Budget(time.monotonic() + 3))[0]
        uid, pid = values.get("UnixUserID"), values.get("ProcessID")
        if type(uid) is not int or type(pid) is not int or pid <= 0:
            raise HelperError("NotAuthorized", "Caller has no Unix process credentials")
        return uid, pid

    def active_graphical(self, uid):
        budget = Budget(time.monotonic() + 5)
        login = "org.freedesktop.login1"
        sessions = self.call(login, "/org/freedesktop/login1", login + ".Manager", "ListSessions", "()", (), budget)[0]
        if len(sessions) > 64:
            return False
        for _, session_uid, _, _, path in sessions:
            if session_uid != uid:
                continue
            values = self.props(login, path, login + ".Session", budget)
            if (values.get("Active") is True and values.get("Remote") is False
                    and values.get("Type") in ("wayland", "x11") and values.get("Class") == "user"
                    and values.get("User", (None,))[0] == uid and values.get("State") == "active"
                    and not values.get("LockedHint", False)):
                return True
        return False

    def rescue_service(self, pid, method):
        budget = Budget(time.monotonic() + 4)
        systemd = "org.freedesktop.systemd1"
        path = self.call(systemd, "/org/freedesktop/systemd1", systemd + ".Manager", "GetUnitByPID", "(u)", (pid,), budget)[0]
        values = self.props(systemd, path, systemd + ".Unit", budget)
        # systemd enters deactivating before SIGTERM; the exact enrolled service
        # must still be able to restore its lease during orderly shutdown.
        state = values.get("ActiveState")
        return values.get("Id") == RESCUE_UNIT and (
            state == "active" or (method == "Release" and state == "deactivating")
        )


class DBusService:
    def __init__(self, helper, authority, bus, Gio, GLib):
        self.helper, self.authority, self.bus = helper, authority, bus
        self.Gio, self.GLib = Gio, GLib
        self.pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="panelbridge-helper")
        self.slots = threading.BoundedSemaphore(3)
        self.stopping = False
        self.cleanup_pending = False
        self.lock = threading.Lock()
        self.contexts = {}
        self.loop = GLib.MainLoop()
        self.node = Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)

    def _return(self, invocation, future, sender, context):
        try:
            result = future.result()
            encoded = json.dumps(result, allow_nan=False, separators=(",", ":"))
            if len(encoded.encode()) > 32768:
                raise HelperError("ResponseTooLarge", "Helper response exceeded its bound")
            invocation.return_value(self.GLib.Variant("(s)", (encoded,)))
        except HelperError as error:
            invocation.return_dbus_error(NAME + "." + error.code, error.message)
        except Exception:
            invocation.return_dbus_error(NAME + ".InternalError", "Helper operation failed")
        finally:
            with self.lock:
                context[1] -= 1
                if context[1] == 0 and self.helper.owner != sender and self.contexts.get(sender) is context:
                    self.contexts.pop(sender, None)
            self.slots.release()
        return False

    def _dispatch(self, sender, method, args, request_cancel):
        self.authority.authorize(sender, method)
        if self.stopping:
            raise HelperError("Stopping", "Helper is shutting down")
        if method == "Status":
            return self.helper.status(refresh=True)
        if method == "Discover":
            return self.helper.discover(sender, request_cancel=request_cancel)
        if method == "Connect":
            return self.helper.connect(sender, args[0])
        if method == "KeepAlive":
            return self.helper.keep_alive(sender)
        return self.helper.release(sender)

    def method_call(self, connection, sender, path, interface, method, parameters, invocation):
        args = parameters.unpack()
        valid = (method in METHODS and (not args if method != "Connect" else
                 len(args) == 1 and isinstance(args[0], str) and bool(re.fullmatch(r"[a-f0-9]{32}", args[0]))))
        if not valid:
            invocation.return_dbus_error(NAME + ".InvalidArguments", "Invalid helper method arguments")
            return
        if self.stopping or not self.slots.acquire(blocking=False):
            invocation.return_dbus_error(NAME + ".Busy", "Helper is processing bounded work")
            return
        with self.lock:
            context = self.contexts.get(sender)
            if context is None or (context[0].is_set() and context[1] == 0 and self.helper.owner != sender):
                context = [threading.Event(), 0]
                self.contexts[sender] = context
            context[1] += 1
        future = self.pool.submit(self._dispatch, sender, method, args, context[0])
        future.add_done_callback(lambda task: self.GLib.idle_add(self._return, invocation, task, sender, context))

    def _cleanup(self, sender=None):
        with self.lock:
            if self.cleanup_pending:
                return
            self.cleanup_pending = True
        def run():
            try:
                if sender:
                    self.helper.release(sender)
                else:
                    self.helper.expire()
            finally:
                with self.lock:
                    self.cleanup_pending = False
        # A dedicated cleanup thread cannot be starved by caller authorization.
        threading.Thread(target=run, name="panelbridge-restore", daemon=True).start()

    def owner_changed(self, connection, sender_name, path, interface, signal_name, parameters, user_data=None):
        name, previous, current = parameters.unpack()
        if previous and not current:
            with self.lock:
                if name in self.contexts:
                    self.contexts[name][0].set()
            if self.helper.cancel_owner(name):
                self._cleanup(name)

    def tick(self):
        status = self.helper.status()
        if status["owner_present"] and time.monotonic() >= status["lease_deadline"]:
            self.helper.cancel_owner(self.helper.owner)
            self._cleanup()
        return not self.stopping

    def stop(self, *_):
        if self.stopping:
            return
        self.stopping = True
        with self.lock:
            for context in self.contexts.values():
                context[0].set()
        sender = self.helper.owner
        if sender:
            self.helper.cancel_owner(sender)
        def shutdown():
            try:
                self.helper.recover()
            finally:
                self.GLib.idle_add(self.loop.quit)
        threading.Thread(target=shutdown, name="panelbridge-shutdown", daemon=True).start()

    def run(self):
        self.helper.recover()
        self.bus.register_object(OBJECT, self.node.interfaces[0], self.method_call, None, None)
        self.bus.signal_subscribe("org.freedesktop.DBus", "org.freedesktop.DBus", "NameOwnerChanged",
                                  "/org/freedesktop/DBus", None, self.Gio.DBusSignalFlags.NONE,
                                  self.owner_changed)
        self.bus.connect("closed", lambda *_: self.stop())
        self.Gio.bus_own_name_on_connection(self.bus, NAME, self.Gio.BusNameOwnerFlags.NONE,
                                           None, lambda *_: self.stop())
        self.GLib.timeout_add_seconds(1, self.tick)
        signal.signal(signal.SIGTERM, self.stop)
        signal.signal(signal.SIGINT, self.stop)
        try:
            self.loop.run()
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)


def main():
    if os.geteuid() != 0 or not sys.flags.isolated:
        raise SystemExit("Use the installed root-owned isolated-mode helper service")
    # Import paths are fixed by the installed launcher; verify our own directory
    # and modules before connecting to the system bus or changing any network.
    directory = Path(__file__).resolve().parent
    directory_fd = _trusted_directory(directory, "InstallationInvalid")
    try:
        for name in ("__init__.py", "network.py", "service.py"):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise HelperError("InstallationInvalid", "Helper source must be root-owned")
    finally:
        os.close(directory_fd)
    load_enrollment()
    backend = GioNetworkBackend()
    helper = NetworkHelper(backend, FileJournal())
    service_bus = backend.new_bus(Budget(time.monotonic() + 5))
    authority = Authority(load_enrollment, GioIdentities(service_bus, backend.Gio, backend.GLib))
    DBusService(helper, authority, service_bus, backend.Gio, backend.GLib).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
