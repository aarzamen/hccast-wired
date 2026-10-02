"""Persistent unprivileged helper owner; system bus calls never block GTK."""

import asyncio
import json
import os
import threading
import time


def bounded_bus_operation(gio, operation, timeout, cancellable=None):
    """GIO cancellation is thread-safe; the timer also covers bus authentication."""
    cancel = cancellable if cancellable is not None else gio.Cancellable.new()
    timer = threading.Timer(timeout, cancel.cancel)
    timer.daemon = True
    timer.start()
    try:
        result = operation(cancel)
        if cancel.is_cancelled():
            raise TimeoutError("Bus operation cancelled or timed out")
        return result
    finally:
        timer.cancel()
        timer.join()


def open_bus(gio, kind, timeout, cancellable=None):
    # Keep ownership even if cancellation races successful construction.
    created = []
    def connect(cancel):
        address = gio.dbus_address_get_for_bus_sync(kind, cancel)
        bus = gio.DBusConnection.new_for_address_sync(
            address,
            gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
            | gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
            None, cancel,
        )
        created.append(bus)
        return bus
    try:
        return bounded_bus_operation(gio, connect, timeout, cancellable)
    except BaseException:
        if created:
            bounded_bus_operation(gio, created[0].close_sync, 3)
        raise


class NetworkClient:
    BUS_TIMEOUT = 5
    SESSION_TIMEOUT = 3

    def __init__(self, *, timeout=None, cancellable=None):
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        self.bus = open_bus(Gio, Gio.BusType.SYSTEM,
                            self.BUS_TIMEOUT if timeout is None else timeout, cancellable)
        self.inflight = set()
        self._session_task = self._session_cancel = None
        self._used = False
        self._closed = False

    def _sync(self, method, arg=None):
        signature = "(s)" if arg is not None else "()"
        args = (arg,) if arg is not None else ()
        timeout = 90000 if method in ("Discover", "Connect", "Release") else 8000
        value = self.bus.call_sync(
            "org.panelbridge.Helper1",
            "/org/panelbridge/Helper1",
            "org.panelbridge.Helper1",
            method,
            self.GLib.Variant(signature, args),
            self.GLib.VariantType.new("(s)"),
            self.Gio.DBusCallFlags.NONE,
            timeout,
            None,
        )
        result = json.loads(value.unpack()[0])
        if not isinstance(result, dict) or result.get("api_version") != 1:
            raise RuntimeError("Unsupported helper response")
        return result

    async def _call(self, method, arg=None):
        if self._closed:
            raise RuntimeError("Helper connection is closed")
        self._used = True
        task = asyncio.create_task(asyncio.to_thread(self._sync, method, arg))
        self.inflight.add(task)

        def completed(done):
            self.inflight.discard(done)
            # Retrieve late failures even after caller cancellation.
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def discover(self):
        return await self._call("Discover")

    async def connect(self, candidate):
        return await self._call("Connect", candidate)

    async def release(self):
        return await self._call("Release")

    async def keep_alive(self):
        return await self._call("KeepAlive")

    async def status(self):
        return await self._call("Status")

    def _session_active(self, deadline, cancel):
        def call(path, interface, method, args):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or cancel.is_cancelled():
                raise TimeoutError("Session check expired")
            return self.bus.call_sync(
                "org.freedesktop.login1",
                path,
                interface,
                method,
                args,
                None,
                self.Gio.DBusCallFlags.NONE,
                max(1, int(remaining * 1000)),
                cancel,
            ).unpack()

        sessions = call(
            "/org/freedesktop/login1", "org.freedesktop.login1.Manager", "ListSessions", None
        )[0]
        for _, uid, _, _, path in sessions:
            if uid != os.getuid():
                continue
            props = call(
                path,
                "org.freedesktop.DBus.Properties",
                "GetAll",
                self.GLib.Variant("(s)", ("org.freedesktop.login1.Session",)),
            )[0]
            if (
                props.get("Active")
                and not props.get("Remote")
                and props.get("Type") in ("wayland", "x11")
                and props.get("Class") == "user"
                and not props.get("LockedHint", False)
            ):
                return time.monotonic() < deadline and not cancel.is_cancelled()
        return False

    async def session_active(self):
        if self._closed:
            return False
        if self._session_task is None:
            self._session_cancel = self.Gio.Cancellable.new()
            deadline = time.monotonic() + self.SESSION_TIMEOUT
            self._session_task = asyncio.create_task(asyncio.to_thread(
                bounded_bus_operation, self.Gio,
                lambda cancel: self._session_active(deadline, cancel),
                self.SESSION_TIMEOUT, self._session_cancel,
            ))
        task, cancel = self._session_task, self._session_cancel
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.cancel()
            # Retain this one task until the underlying GIO call acknowledges
            # cancellation; a later caller cannot allocate another thread.
            raise
        except Exception:
            return False
        finally:
            if task.done() and self._session_task is task:
                self._session_task = self._session_cancel = None
                if not task.cancelled():
                    task.exception()

    async def close(self):
        if self._closed:
            return
        if self._session_cancel:
            self._session_cancel.cancel()
        try:
            if self._used:
                await self.release()
        finally:
            self._closed = True
            try:
                await asyncio.to_thread(
                    bounded_bus_operation, self.Gio, self.bus.close_sync, self.BUS_TIMEOUT
                )
            finally:
                pending = list(self.inflight)
                if self._session_task:
                    pending.append(self._session_task)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self._session_task = self._session_cancel = None
