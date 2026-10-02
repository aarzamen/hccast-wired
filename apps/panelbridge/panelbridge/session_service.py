"""Normal-user D-Bus controller service with a separate asynchronous supervisor."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import signal
import sys
import threading
import time

from .controller import Controller
from .health import HealthMonitor, safety_action
from .media import DesktopMedia, ProcessGroup
from .network_client import NetworkClient, bounded_bus_operation, open_bus
from .output import OutputAdapter
from .state import StateStore
from .startup_choice import read_startup_choice, autostart_enabled

NAME = "org.panelbridge.Session1"
OBJECT = "/org/panelbridge/Session1"
XML = """<node><interface name="org.panelbridge.Session1">
<method name="GetState"><arg type="s" name="state" direction="out"/></method>
<method name="Command"><arg type="s" name="verb" direction="in"/><arg type="s" name="payload" direction="in"/><arg type="s" name="result" direction="out"/></method>
<signal name="StateChanged"><arg type="s" name="state"/></signal>
</interface></node>"""


UNKNOWN_HEALTH = {"temperature_c": None, "cpu_percent": None, "throttled_bits": None}
HEALTH_SAMPLER = """
import sys
sys.path.insert(0, sys.argv[1])
from panelbridge.session_service import health_sampler_main
health_sampler_main()
"""


def health_sampler_main():
    monitor = HealthMonitor()
    while request := sys.stdin.readline(32):
        if request == "sample\n":
            value = monitor.sample()
        elif request == "sample_timed\n":
            value = monitor.sample_timed()
        else:
            raise SystemExit("Invalid sample request")
        print(json.dumps(value, allow_nan=False), flush=True)


def _unique_health_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate health sample field")
        result[key] = value
    return result


def _validate_health_sample(value, *, timed):
    fields = set(UNKNOWN_HEALTH) | ({"monotonic_us", "cpu_interval"} if timed else set())
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("Invalid health sample shape")
    for name, low, high, integer in (("temperature_c", -20, 150, False),
                                     ("cpu_percent", 0, 100, False),
                                     ("throttled_bits", 0, 0xFFFFFFFF, True)):
        number = value[name]
        if number is not None and (
                (type(number) is not int if integer else type(number) not in (int, float))
                or not low <= number <= high or not math.isfinite(number)):
            raise ValueError("Invalid health measurement")
    if timed:
        at_us = value["monotonic_us"]
        if type(at_us) is not int or not 0 <= at_us <= 2**63 - 1:
            raise ValueError("Invalid producer health timestamp")
        interval = value["cpu_interval"]
        if interval is None:
            if value["cpu_percent"] is not None:
                raise ValueError("CPU measurement is missing its producer interval")
        elif (not isinstance(interval, dict) or set(interval) != {"start_us", "end_us"}
              or any(type(interval[key]) is not int for key in ("start_us", "end_us"))
              or not 0 <= interval["start_us"] < interval["end_us"] <= at_us
              or value["cpu_percent"] is None):
            raise ValueError("Invalid producer CPU interval")
    # Preserve raw unknown firmware flags for the supervisor's fail-closed policy.
    safety_action(value)
    return value


class HealthSampler:
    """One owned unprivileged sampler process, shared across consecutive samples."""

    def __init__(self, timeout=3):
        self.timeout = timeout
        self.processes = ProcessGroup()
        self.process = None
        self._pending = None

    async def sample(self):
        """Legacy three-field health, sharing the timed producer's CPU history."""
        value = await self._request(timed=False)
        return {key: value[key] for key in UNKNOWN_HEALTH}

    async def sample_timed(self):
        """Five producer fields, unchanged by command or IPC delivery latency."""
        return await self._request(timed=True)

    async def _request(self, *, timed):
        if self._pending is not None:
            raise RuntimeError("A health sample is already running")
        task = self._pending = asyncio.create_task(self._sample(timed=timed))
        try:
            return await asyncio.wait_for(asyncio.shield(task), self.timeout)
        except BaseException:
            await self.close()
            raise
        finally:
            if self._pending is task:
                self._pending = None

    async def _sample(self, *, timed=False):
        if self.process is None:
            self.process = await self.processes.spawn([
                sys.executable, "-I", "-B", "-c", HEALTH_SAMPLER,
                str(Path(__file__).resolve().parents[1]),
            ], stdin=asyncio.subprocess.PIPE, limit=4096)
        self.process.stdin.write(b"sample_timed\n" if timed else b"sample\n")
        await self.process.stdin.drain()
        raw = await self.process.stdout.readline()
        if not raw or len(raw) > 4096:
            raise ValueError("Health sampler failed")
        try:
            value = json.loads(raw, object_pairs_hook=_unique_health_object)
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError("Invalid health sample encoding") from None
        return _validate_health_sample(value, timed=timed)

    async def close(self):
        if self._pending is not None:
            self._pending.cancel()
            await asyncio.gather(self._pending, return_exceptions=True)
        await self.processes.close(grace=0.2, kill_grace=1)
        self.process = None


class SessionService:
    STARTUP_TIMEOUT = 8
    TICK_INTERVAL = 0.25
    SESSION_INTERVAL = 1
    SESSION_TIMEOUT = 3
    HEALTH_INTERVAL = 2
    HEALTH_TIMEOUT = 3
    NETWORK_INTERVAL = 2
    NETWORK_TIMEOUT = 6
    STARTUP_NETWORK_TIMEOUT = 60

    def __init__(self, args):
        from gi.repository import Gio, GLib

        self.Gio, self.GLib = Gio, GLib
        self.args = args
        self.main_loop = GLib.MainLoop()
        self.async_loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        self.startup_cancel = Gio.Cancellable.new()
        self.init_error = None
        self.shutting_down = self.name_owned = False
        self.controller = self.network = self.bus = self.thread = None
        self.watch = self._cleanup_task = None
        self._running_main = False
        self.registration = self.owner = self.name_timer = None
        # Set once so Stop can cancel it even before the name-acquired watch runs.
        self.startup_choice = read_startup_choice()
        self._startup_choice_message_pending = self.startup_choice in ('desktop', 'headless')
        self._autostart_pending = autostart_enabled(args.no_autostart, self.startup_choice)
        try:
            self.bus = open_bus(Gio, Gio.BusType.SESSION, self.STARTUP_TIMEOUT,
                                self.startup_cancel)
            self.thread = threading.Thread(target=self._async_main, name="panelbridge-supervisor")
            self.thread.start()
            if not self.ready.wait(self.STARTUP_TIMEOUT):
                raise RuntimeError("Controller startup timed out")
            if self.init_error:
                raise self.init_error
            self.node = Gio.DBusNodeInfo.new_for_xml(XML)
            self.registration = self.bus.register_object(
                OBJECT, self.node.interfaces[0], self.method, None, None
            )
            self.name_timer = GLib.timeout_add(int(self.STARTUP_TIMEOUT * 1000),
                                              self._name_timeout)
            self.owner = Gio.bus_own_name_on_connection(
                self.bus, NAME, Gio.BusNameOwnerFlags.DO_NOT_QUEUE,
                self._name_acquired, self._name_lost,
            )
            self.bus.connect("closed", lambda *_: self.stop())
            signal.signal(signal.SIGTERM, lambda *_: self.stop())
            signal.signal(signal.SIGINT, lambda *_: self.stop())
            if args.seconds:
                GLib.timeout_add_seconds(args.seconds, self.stop)
        except BaseException:
            self.shutting_down = True
            self.startup_cancel.cancel()
            if self.thread is not None:
                if not self.async_loop.is_closed():
                    self.async_loop.call_soon_threadsafe(self._abort_startup_loop)
                self.thread.join(self.STARTUP_TIMEOUT + 5)
                if self.thread.is_alive():
                    raise RuntimeError("Controller startup cleanup did not finish") from None
            else:
                self.async_loop.close()
            self._dispose_bus()
            raise

    def _abort_startup_loop(self):
        # A queued abort must not interrupt run_until_complete(cleanup) when
        # bus cancellation returned before run_forever ever began.
        if self._running_main:
            self.async_loop.stop()

    def _name_acquired(self, *_):
        if self.shutting_down:
            return
        self.name_owned = True
        if self.name_timer:
            self.GLib.source_remove(self.name_timer)
            self.name_timer = None
        self.async_loop.call_soon_threadsafe(self._start_watch)

    def _name_lost(self, *_):
        self.name_owned = False
        self.stop()

    def _name_timeout(self):
        self.name_timer = None
        if not self.name_owned:
            self.stop()
        return False

    def _start_watch(self):
        if self.name_owned and not self.shutting_down and self.watch is None:
            self.watch = self.async_loop.create_task(self.watch_state())
            self.watch.add_done_callback(self._watch_done)

    def _watch_done(self, task):
        if not task.cancelled() and task.exception() is not None:
            print("Controller supervisor failed:", task.exception(), flush=True)
            self.GLib.idle_add(self.stop)

    def _async_main(self):
        asyncio.set_event_loop(self.async_loop)
        try:
            self.network = NetworkClient(timeout=self.STARTUP_TIMEOUT,
                                         cancellable=self.startup_cancel)
            if self.shutting_down or self.startup_cancel.is_cancelled():
                return
            media = DesktopMedia(OutputAdapter(), self.args.worker,
                                 continuous=not self.args.bounded_media, seconds=60)
            self.controller = Controller(StateStore(self.args.state), self.network, media)
            if self.startup_choice in ('desktop', 'headless') and not self.controller.store.config_error:
                self.controller.message = 'Monitor stopped by startup choice. Press Start to connect.'
            self.controller.session_unlocked = False
            self.ready.set()
            if not self.shutting_down:
                self._running_main = True
                self.async_loop.run_forever()
        except BaseException as error:
            self.init_error = error
        finally:
            self._running_main = False
            self.ready.set()
            try:
                self.async_loop.run_until_complete(self.shutdown())
            except Exception as error:
                print("Controller cleanup:", error, flush=True)
            pending = asyncio.all_tasks(self.async_loop)
            for task in pending:
                task.cancel()
            if pending:
                self.async_loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            self.async_loop.run_until_complete(self.async_loop.shutdown_asyncgens())
            self.async_loop.run_until_complete(self.async_loop.shutdown_default_executor(timeout=10))
            self.async_loop.close()

    async def _sample_health(self):
        return await self.health_sampler.sample()

    async def watch_state(self):
        self.health_sampler = HealthSampler(timeout=self.HEALTH_TIMEOUT)
        self._session_valid = False
        self._health_valid = False
        self._network_ready = False
        network_deadline = None
        self.controller.session_unlocked = False
        polls = {
            "session": {"task": None, "next": 0, "deadline": 0, "expired": False,
                        "interval": self.SESSION_INTERVAL, "timeout": self.SESSION_TIMEOUT,
                        "call": self.controller.network.session_active},
            "health": {"task": None, "next": 0, "deadline": 0, "expired": False,
                       "interval": self.HEALTH_INTERVAL, "timeout": self.HEALTH_TIMEOUT,
                       "call": self._sample_health},
            "network": {"task": None, "next": 0, "deadline": 0, "expired": False,
                        "interval": self.NETWORK_INTERVAL, "timeout": self.NETWORK_TIMEOUT,
                        "call": self.controller.network.status},
        }
        safety_task = None
        was_permitted = False
        self._resume_after_session = False
        stop_pending = False
        try:
            while not self.shutting_down:
                now = time.monotonic()
                for kind, poll in polls.items():
                    task = poll["task"]
                    if task is not None and task.done():
                        # Clear ownership before consuming exceptions, so one bad
                        # result cannot poison every subsequent supervisor tick.
                        poll["task"] = None
                        poll["next"] = now + poll["interval"]
                        try:
                            value = task.result()
                            if poll["expired"] or now >= poll["deadline"]:
                                raise TimeoutError("Safety sample expired")
                        except (Exception, asyncio.CancelledError):
                            value = dict(UNKNOWN_HEALTH) if kind == "health" else False
                        if kind == "session":
                            self._session_valid = value is True
                        elif kind == "health":
                            self.controller.health = value
                            self._health_valid = safety_action(value) == "ok"
                        else:
                            self._network_ready = (
                                isinstance(value, dict) and value.get("api_version") == 1
                                and value.get("recovery_available") is True
                                and value.get("owner_present") is False
                                and value.get("state") in ("idle", "restore_required")
                            )
                    elif task is not None and now >= poll["deadline"] and not poll["expired"]:
                        poll["expired"] = True
                        task.cancel()
                        if kind == "session":
                            self._session_valid = False
                        elif kind == "health":
                            self._health_valid = False
                            self.controller.health = dict(UNKNOWN_HEALTH)
                        else:
                            self._network_ready = False
                    needs_poll = kind != "network" or self._autostart_pending or self._resume_after_session
                    if needs_poll and poll["task"] is None and now >= poll["next"]:
                        poll["deadline"] = now + poll["timeout"]
                        poll["expired"] = False
                        poll["task"] = asyncio.create_task(poll["call"]())

                if safety_task is not None and safety_task.done():
                    finished, safety_task = safety_task, None
                    try:
                        finished.result()
                    except Exception as error:
                        self.controller.message = str(error)

                permitted = self.name_owned and self._session_valid and self._health_valid
                if not permitted:
                    if was_permitted and not self._session_valid and self._health_valid:
                        self._resume_after_session = self.controller.desired
                    if not self._health_valid:
                        self._resume_after_session = False
                    stop_pending = stop_pending or was_permitted or self.controller.desired
                    # These public controller gates invalidate pending starts
                    # immediately, without waiting behind a slow cleanup/command.
                    self.controller.session_unlocked = False
                    self.controller.desired = self.controller.resume_after_unlock = False
                    self.controller.message = (
                        "Desktop locked or unavailable; capture is paused"
                        if not self._session_valid else
                        "Pi health is unavailable or unsafe; capture is paused"
                    )
                elif safety_task is None and not stop_pending:
                    self.controller.session_unlocked = True
                    if getattr(self, '_startup_choice_message_pending', False):
                        self._startup_choice_message_pending = False
                        if not self.controller.store.config_error and not self.controller.store.recovery_required:
                            self.controller.message = 'Monitor stopped by startup choice. Press Start to connect.'
                    if self.controller.store.config_error or self.controller.store.recovery_required:
                        self._autostart_pending = self._resume_after_session = False
                    operation = getattr(self.controller, "operation", None)
                    if (self._autostart_pending or self._resume_after_session) and (
                        operation is None or operation.done()
                    ):
                        if network_deadline is None:
                            network_deadline = now + self.STARTUP_NETWORK_TIMEOUT
                        if self._network_ready:
                            self._autostart_pending = self._resume_after_session = False
                            network_deadline = None
                            self._network_ready = False
                            safety_task = asyncio.create_task(self.controller.command("start", {}))
                        elif now >= network_deadline:
                            self._autostart_pending = self._resume_after_session = False
                            network_deadline = None
                            self.controller.status = "error"
                            self.controller.message = "Ethernet recovery is not ready; connect Ethernet and press Start"
                        else:
                            self.controller.message = "Waiting for Ethernet recovery before connecting"
                if not (self._autostart_pending or self._resume_after_session):
                    network_deadline = None
                    self._network_ready = False
                if stop_pending and safety_task is None:
                    stop_pending = False
                    safety_task = asyncio.create_task(self.controller.command("stop", {}))
                was_permitted = permitted
                try:
                    await self.controller.tick()
                except Exception as error:
                    self.controller.message = str(error)
                self.GLib.idle_add(self.emit, json.dumps(self.controller.snapshot(), allow_nan=False))
                await asyncio.sleep(self.TICK_INTERVAL)
        finally:
            tasks = [poll["task"] for poll in polls.values() if poll["task"] is not None]
            if safety_task:
                tasks.append(safety_task)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self.health_sampler.close()

    def emit(self, body):
        if not self.shutting_down:
            self.bus.emit_signal(
                None, OBJECT, NAME, "StateChanged", self.GLib.Variant("(s)", (body,))
            )
        return False

    async def dispatch(self, method, args):
        if method == "GetState":
            return self.controller.snapshot()
        if method != "Command" or len(args) != 2 or len(args[1]) > 16384:
            return {"api_version": 1, "ok": False, "error": "Invalid command"}
        try:
            payload = json.loads(args[1])
            if args[0] == "start" and (
                not self.name_owned or not self.controller.session_unlocked
                or safety_action(self.controller.health) != "ok"
            ):
                raise ValueError(
                    "Unlock the desktop and wait for safe Pi health readings before streaming"
                )
            if (args[0] in ("start", "stop", "restore_defaults")
                    and isinstance(payload, dict) and not payload):
                self._autostart_pending = self._resume_after_session = False
            result = await self.controller.command(args[0], payload)
            return {"api_version": 1, "ok": True, "result": result}
        except (ValueError, RuntimeError) as error:
            return {"api_version": 1, "ok": False, "error": str(error)}

    def method(self, connection, sender, path, interface, method, parameters, invocation):
        if self.shutting_down or not self.name_owned:
            invocation.return_dbus_error(NAME + ".Stopping", "Controller is stopping")
            return
        future = asyncio.run_coroutine_threadsafe(
            self.dispatch(method, parameters.unpack()), self.async_loop
        )

        def done(task):
            self.GLib.idle_add(self.respond, invocation, task)

        future.add_done_callback(done)

    def respond(self, invocation, future):
        try:
            result = future.result()
            invocation.return_value(
                self.GLib.Variant("(s)", (json.dumps(result, allow_nan=False),))
            )
        except Exception:
            invocation.return_dbus_error(NAME + ".Failed", "Controller operation failed")
        return False

    async def shutdown(self):
        if self._cleanup_task is None:
            self._cleanup_task = asyncio.create_task(self._shutdown())
        await asyncio.shield(self._cleanup_task)

    async def _shutdown(self):
        if self.watch:
            self.watch.cancel()
            await asyncio.gather(self.watch, return_exceptions=True)
        try:
            if self.controller:
                await self.controller.close()
        finally:
            if self.network:
                await self.network.close()

    def stop(self):
        if self.shutting_down:
            return False
        self.shutting_down = True
        self.name_owned = False
        self.startup_cancel.cancel()
        future = asyncio.run_coroutine_threadsafe(self.shutdown(), self.async_loop)

        def done(task):
            try:
                task.result()
            except Exception as error:
                print("Controller cleanup:", error, flush=True)
            self.async_loop.call_soon_threadsafe(self.async_loop.stop)
            self.GLib.idle_add(self.main_loop.quit)

        future.add_done_callback(done)
        return False

    def _dispose_bus(self):
        if self.name_timer:
            self.GLib.source_remove(self.name_timer)
            self.name_timer = None
        if self.registration:
            self.bus.unregister_object(self.registration)
            self.registration = None
        if self.owner:
            self.Gio.bus_unown_name(self.owner)
            self.owner = None
        if self.bus and not self.bus.is_closed():
            bounded_bus_operation(self.Gio, self.bus.close_sync, 3)

    def run(self):
        try:
            self.main_loop.run()
        finally:
            self.stop()
            self.thread.join(timeout=100)
            self._dispose_bus()
            if self.thread.is_alive():
                raise RuntimeError("Controller cleanup did not finish")


def main():
    if os.geteuid() == 0:
        raise SystemExit("Run PanelBridge in the normal desktop account")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state", type=Path, default=Path.home() / ".config/panelbridge/session.json"
    )
    parser.add_argument(
        "--worker", type=Path, default=Path("/usr/lib/panelbridge/bin/panelbridge-wfd-worker")
    )
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument(
        "--bounded-media", action="store_true", help="Development: use a 60-second worker"
    )
    parser.add_argument("--seconds", type=int, default=0, help="Development service lifetime")
    args = parser.parse_args()
    if args.seconds < 0 or args.seconds > 14400:
        parser.error("Lifetime must be zero or at most four hours")
    SessionService(args).run()


if __name__ == "__main__":
    main()
