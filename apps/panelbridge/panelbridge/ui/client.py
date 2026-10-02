"""Asynchronous, bounded client for the enrolled user's session controller."""

from __future__ import annotations

import json
from typing import Any, Callable

import gi

from .presenter import decode_object, decode_result

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402 (version must precede GI import)


BUS_NAME = "org.panelbridge.Session1"
OBJECT_PATH = "/org/panelbridge/Session1"
INTERFACE = "org.panelbridge.Session1"


class SessionClient:
    def __init__(self, on_state: Callable[[dict[str, Any]], None], on_error: Callable[[str], None]):
        self.on_state = on_state
        self.on_error = on_error
        self.proxy: Gio.DBusProxy | None = None
        self.cancel: Gio.Cancellable | None = None
        self._generation = 0
        self._state_generation = 0
        self._owner_epoch = 0
        self._poll = 0
        self._startup = 0
        self._fetching = False
        self._signals: list[tuple[Any, int]] = []

    def connect(self) -> None:
        self.close()
        generation = self._generation
        self.cancel = Gio.Cancellable()
        self._startup = GLib.timeout_add_seconds(6, self._startup_expired, generation)
        Gio.DBusProxy.new_for_bus(
            Gio.BusType.SESSION, Gio.DBusProxyFlags.DO_NOT_AUTO_START, None,
            BUS_NAME, OBJECT_PATH, INTERFACE, self.cancel, self._ready, generation,
        )

    def _startup_expired(self, generation: int) -> bool:
        self._startup = 0
        if generation == self._generation and self.proxy is None:
            if self.cancel:
                self.cancel.cancel()
            self.on_error("The desktop session service did not respond. Retry the connection.")
        return GLib.SOURCE_REMOVE

    def _ready(self, _source: Any, result: Gio.AsyncResult, generation: int) -> None:
        if generation != self._generation:
            return
        if self._startup:
            GLib.source_remove(self._startup)
            self._startup = 0
        try:
            self.proxy = Gio.DBusProxy.new_for_bus_finish(result)
        except GLib.Error as error:
            self.on_error(f"Cannot reach the desktop session service: {error.message}")
            return
        self._signals = [
            (self.proxy, self.proxy.connect("g-signal", self._signal)),
            (self.proxy, self.proxy.connect("notify::g-name-owner", self._owner_changed)),
        ]
        connection = self.proxy.get_connection()
        self._signals.append((connection, connection.connect("closed", self._bus_closed)))
        self._poll = GLib.timeout_add_seconds(2, self._poll_state)
        self._owner_changed(self.proxy, None)

    def _owner_changed(self, proxy: Gio.DBusProxy, _spec: Any) -> None:
        self._owner_epoch += 1
        self._fetching = False
        if not proxy.get_name_owner():
            self.on_error("The PanelBridge session controller is unavailable. Retry after it starts.")
        else:
            self.refresh()

    def _bus_closed(self, _connection: Any, _remote: bool, _error: Any) -> None:
        self.close()
        self.on_error("The desktop session connection closed. Retry to reconnect.")

    def _signal(self, _proxy: Any, _sender: str, name: str, parameters: GLib.Variant) -> None:
        if name != "StateChanged":
            return
        self._state_generation += 1
        try:
            self.on_state(decode_object(parameters.unpack()[0]))
        except (ValueError, TypeError, IndexError) as error:
            self.on_error(str(error))

    def _poll_state(self) -> bool:
        self.refresh()
        return GLib.SOURCE_CONTINUE

    def refresh(self) -> None:
        if self._fetching or not self.proxy or not self.proxy.get_name_owner():
            return
        self._fetching = True
        generation, state_generation = self._generation, self._state_generation
        owner_epoch = self._owner_epoch

        def received(proxy: Gio.DBusProxy, result: Gio.AsyncResult, _data: Any) -> None:
            if generation != self._generation or owner_epoch != self._owner_epoch:
                return
            self._fetching = False
            try:
                value = decode_object(proxy.call_finish(result).unpack()[0])
                if state_generation == self._state_generation:
                    self.on_state(value)
            except (GLib.Error, ValueError, TypeError, IndexError) as error:
                self.on_error(f"Could not read display status: {error}")

        try:
            self.proxy.call("GetState", None, Gio.DBusCallFlags.NO_AUTO_START,
                            5000, self.cancel, received, None)
        except GLib.Error as error:
            self._fetching = False
            self.on_error(f"Could not read display status: {error.message}")

    def command(self, verb: str, payload: dict[str, Any],
                done: Callable[[dict[str, Any] | None, str | None], None]) -> None:
        if not self.proxy or not self.proxy.get_name_owner():
            done(None, "The session controller is unavailable. Retry the connection.")
            return
        generation = self._generation
        owner_epoch = self._owner_epoch

        def received(proxy: Gio.DBusProxy, result: Gio.AsyncResult, _data: Any) -> None:
            if generation != self._generation or owner_epoch != self._owner_epoch:
                return
            try:
                value = decode_result(proxy.call_finish(result).unpack()[0])
            except (GLib.Error, ValueError, TypeError, IndexError) as error:
                done(None, str(error))
            else:
                done(value, None)
            self.refresh()

        try:
            parameters = GLib.Variant("(ss)", (verb, json.dumps(payload, allow_nan=False)))
            self.proxy.call("Command", parameters, Gio.DBusCallFlags.NO_AUTO_START,
                            30000, self.cancel, received, None)
        except (GLib.Error, ValueError, TypeError) as error:
            done(None, str(error))

    def close(self) -> None:
        self._generation += 1
        if self.cancel:
            self.cancel.cancel()
        for timer in (self._startup, self._poll):
            if timer:
                GLib.source_remove(timer)
        self._startup = self._poll = 0
        for obj, signal_id in self._signals:
            obj.disconnect(signal_id)
        self._signals.clear()
        self.proxy = None
        self.cancel = None
        self._fetching = False
