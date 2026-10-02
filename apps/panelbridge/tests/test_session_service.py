"""Supervisor lifecycle tests; GI/network/hardware boundaries are deterministic fakes."""

import asyncio
import json
import os
from pathlib import Path
import sys
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge import session_service as service_module
from panelbridge import network_client as client_module

HEALTHY = {"temperature_c": 45, "cpu_percent": 20, "throttled_bits": 0}


class Cancellable:
    def __init__(self):
        self.event = threading.Event()

    @classmethod
    def new(cls):
        return cls()

    def cancel(self):
        self.event.set()

    def is_cancelled(self):
        return self.event.is_set()


class Bus:
    def __init__(self):
        self.closed = False
        self.registered = False

    def register_object(self, *args):
        self.registered = True
        return 1

    def unregister_object(self, *args):
        self.registered = False

    def connect(self, *args):
        return 1

    def close_sync(self, cancel):
        self.closed = True

    def is_closed(self):
        return self.closed

    def emit_signal(self, *args):
        pass


@pytest.fixture
def fake_gi(monkeypatch):
    buses = []
    callbacks = {}
    def connection(address, flags, observer, cancel):
        bus = Bus()
        buses.append(bus)
        return bus
    def own(bus, name, flags, acquired, lost):
        callbacks.update(acquired=acquired, lost=lost)
        return 1
    class MainLoop:
        def __init__(self): self.done = threading.Event()
        def run(self): self.done.wait(2)
        def quit(self): self.done.set()
    gio = SimpleNamespace(
        Cancellable=Cancellable,
        BusType=SimpleNamespace(SESSION=1, SYSTEM=2),
        DBusConnectionFlags=SimpleNamespace(AUTHENTICATION_CLIENT=1, MESSAGE_BUS_CONNECTION=2),
        DBusCallFlags=SimpleNamespace(NONE=0),
        BusNameOwnerFlags=SimpleNamespace(NONE=0, DO_NOT_QUEUE=4),
        DBusConnection=SimpleNamespace(new_for_address_sync=connection),
        bus_get_sync=lambda *args: connection(None, None, None, None),
        dbus_address_get_for_bus_sync=lambda kind, cancel: "unix:path=/synthetic/bus",
        DBusNodeInfo=SimpleNamespace(new_for_xml=lambda _: SimpleNamespace(interfaces=[object()])),
        bus_own_name_on_connection=own,
        bus_unown_name=lambda _: None,
    )
    glib = SimpleNamespace(
        MainLoop=MainLoop,
        idle_add=lambda fn, *args: fn(*args),
        timeout_add=lambda *args: 1,
        timeout_add_seconds=lambda *args: 1,
        source_remove=lambda _: None,
        Variant=lambda signature, value: value,
        VariantType=SimpleNamespace(new=lambda value: value),
    )
    repository = SimpleNamespace(Gio=gio, GLib=glib)
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(repository=repository))
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setattr(service_module.signal, "signal", lambda *args: None)
    return SimpleNamespace(Gio=gio, buses=buses, callbacks=callbacks)


class Network:
    def __init__(self, **kwargs):
        self.closed = False
        self.poll_calls = 0
        self.block = None
        self.recovery_available = True
        self.status_calls = 0

    async def status(self):
        self.status_calls += 1
        return {"api_version": 1, "state": "idle", "owner_present": False,
                "recovery_available": self.recovery_available}

    async def session_active(self):
        self.poll_calls += 1
        if self.block:
            await self.block.wait()
        return True

    async def release(self): return {"network_restored": True}
    async def close(self): self.closed = True


class Controller:
    def __init__(self, store, network, media):
        self.store = SimpleNamespace(selected_device={"name": "synthetic"},
                                     config_error=None, recovery_required=False)
        self.network = network
        self.health = dict(HEALTHY)
        self.session_unlocked = True
        self.resume_after_unlock = self.desired = False
        self.message = ""
        self.starts = self.stops = self.ticks = 0
        self.closed = False

    async def command(self, verb, payload):
        if verb == "start":
            self.starts += 1
            self.desired = True
        elif verb == "stop":
            self.stops += 1
            self.desired = self.resume_after_unlock = False
        return {"accepted": True}

    async def session_changed(self, active):
        self.session_unlocked = active
        if not active:
            self.desired = False
            self.stops += 1

    async def tick(self): self.ticks += 1
    async def close(self): self.closed = True
    def snapshot(self): return {}


def args(tmp_path):
    return SimpleNamespace(state=tmp_path / "state.json", worker=tmp_path / "worker",
                           bounded_media=True, no_autostart=False, seconds=0)


def dependencies(monkeypatch):
    monkeypatch.setattr(service_module, "Controller", Controller)
    monkeypatch.setattr(service_module, "DesktopMedia", lambda *a, **k: None)
    monkeypatch.setattr(service_module, "OutputAdapter", lambda: None)
    monkeypatch.setattr(service_module, "NetworkClient", Network)
    async def healthy(self): return dict(HEALTHY)
    monkeypatch.setattr(service_module.SessionService, "_sample_health", healthy, raising=False)
    monkeypatch.setattr(service_module, "HealthMonitor", lambda: SimpleNamespace(sample=lambda: HEALTHY), raising=False)


def finish_service(service):
    service.stop()
    service.thread.join(3)
    assert not service.thread.is_alive()


def test_saved_autostart_waits_for_name_acquisition(tmp_path, monkeypatch, fake_gi):
    dependencies(monkeypatch)
    service = service_module.SessionService(args(tmp_path))
    try:
        time.sleep(0.1)
        assert service.controller.starts == 0
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        until = time.monotonic() + 2
        while not service.controller.starts and time.monotonic() < until:
            time.sleep(0.01)
        assert service.controller.starts == 1
    finally:
        finish_service(service)


def test_startup_timeout_cancels_and_joins_late_constructor(tmp_path, monkeypatch, fake_gi):
    dependencies(monkeypatch)
    monkeypatch.setattr(service_module.SessionService, "STARTUP_TIMEOUT", 0.03, raising=False)
    class SlowNetwork(Network):
        def __init__(self, **kwargs):
            super().__init__()
            cancel = kwargs.get("cancellable")
            if cancel:
                cancel.event.wait(0.3)
            else:
                time.sleep(0.12)
    monkeypatch.setattr(service_module, "NetworkClient", SlowNetwork)
    service = object.__new__(service_module.SessionService)
    try:
        with pytest.raises((RuntimeError, TimeoutError), match="startup|Startup|cancelled"):
            service.__init__(args(tmp_path))
        assert not service.thread.is_alive()
        assert service.controller is None or service.controller.starts == 0
        assert all(bus.closed for bus in fake_gi.buses)
        assert service.network is None or service.network.closed
    finally:
        if hasattr(service, "thread") and service.thread.is_alive():
            finish_service(service)


def bare_service(monkeypatch):
    service = object.__new__(service_module.SessionService)
    service.args = SimpleNamespace(no_autostart=True)
    service.controller = Controller(None, Network(), None)
    service.shutting_down = False
    service.name_owned = True
    service.GLib = SimpleNamespace(idle_add=lambda *a: None)
    service.SESSION_INTERVAL = service.HEALTH_INTERVAL = 0.03
    service.SESSION_TIMEOUT = service.HEALTH_TIMEOUT = 0.07
    service.TICK_INTERVAL = 0.01
    service._autostart_pending = False
    return service


def test_failed_health_sample_does_not_disable_ticks(monkeypatch):
    async def scenario():
        service = bare_service(monkeypatch)
        class Failed:
            def sample(self): raise UnicodeError("bad sensor text")
        monkeypatch.setattr(service_module, "HealthMonitor", Failed, raising=False)
        async def failed(self): raise UnicodeError("bad sensor text")
        monkeypatch.setattr(service_module.SessionService, "_sample_health", failed, raising=False)
        task = asyncio.create_task(service.watch_state())
        try:
            await asyncio.sleep(0.58)
            before = service.controller.ticks
            await asyncio.sleep(0.3)
            assert service.controller.ticks > before
            assert not service.controller.session_unlocked
        finally:
            service.shutting_down = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_slow_session_poll_does_not_block_tick_and_expires_closed(monkeypatch):
    async def scenario():
        service = bare_service(monkeypatch)
        service.controller.network.block = asyncio.Event()
        service.controller.desired = True
        async def healthy(self): return dict(HEALTHY)
        monkeypatch.setattr(service_module.SessionService, "_sample_health", healthy, raising=False)
        task = asyncio.create_task(service.watch_state())
        try:
            await asyncio.sleep(0.18)
            assert service.controller.ticks >= 5
            assert not service.controller.session_unlocked
            assert not service.controller.desired
        finally:
            service.shutting_down = True
            task.cancel()
            service.controller.network.block.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_unknown_health_rejects_manual_start(monkeypatch):
    async def scenario():
        service = bare_service(monkeypatch)
        service.controller.health = {"temperature_c": None, "throttled_bits": None}
        result = await service.dispatch("Command", ("start", "{}"))
        assert not result["ok"]
        assert service.controller.starts == 0
    asyncio.run(scenario())


def test_network_bus_constructor_has_cancellable_deadline(fake_gi, monkeypatch):
    monkeypatch.setattr(client_module.NetworkClient, "BUS_TIMEOUT", 0.03, raising=False)
    def blocked(address, flags, observer, cancel):
        if cancel is None:
            raise AssertionError("Bus creation lacks a cancellable")
        assert cancel.event.wait(0.5)
        raise TimeoutError("cancelled")
    fake_gi.Gio.DBusConnection.new_for_address_sync = blocked
    with pytest.raises(TimeoutError, match="cancelled"):
        client_module.NetworkClient()


def test_sampler_keeps_cpu_history_between_requests(monkeypatch, capsys):
    import io
    from panelbridge.health import HealthMonitor
    counters = iter(("cpu 10 0 10 80 0 0 0 0\n", "cpu 20 0 20 100 0 0 0 0\n"))
    def read(path):
        return next(counters) if path == "/proc/stat" else "45000"
    monkeypatch.setattr(service_module, "HealthMonitor", lambda: HealthMonitor(
        read=read, throttle=lambda: "throttled=0x0\n"), raising=False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("sample\nsample\n"))
    service_module.health_sampler_main()
    samples = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert samples[0]["cpu_percent"] is None
    assert samples[1]["cpu_percent"] == 50


def test_sampler_deadline_reaps_stuck_child_and_can_retry(monkeypatch):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", "import time; time.sleep(60)")
        sampler = service_module.HealthSampler(timeout=0.05)
        try:
            with pytest.raises(TimeoutError):
                await sampler.sample()
            assert not sampler.processes.children
            monkeypatch.setattr(service_module, "HEALTH_SAMPLER", (
                "import sys; "
                "[(print('{\\\"temperature_c\\\":45,\\\"cpu_percent\\\":20,"
                "\\\"throttled_bits\\\":0}',flush=True)) for x in sys.stdin]"
            ))
            sampler.timeout = 1
            assert await sampler.sample() == HEALTHY
            pid = sampler.process.pid
            assert await sampler.sample() == HEALTHY
            assert sampler.process.pid == pid
        finally:
            await sampler.close()
        assert not sampler.processes.children
    asyncio.run(scenario())


def test_sampler_cancellation_joins_owned_child(monkeypatch):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", "import time; time.sleep(60)")
        sampler = service_module.HealthSampler(timeout=2)
        task = asyncio.create_task(sampler.sample())
        while not sampler.processes.children:
            await asyncio.sleep(0.001)
        child = sampler.processes.children[0]
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await sampler.close()
        assert child.returncode is not None and not sampler.processes.children
    asyncio.run(scenario())


def test_session_check_has_one_total_budget_across_sessions(fake_gi):
    async def scenario():
        client = client_module.NetworkClient()
        client.SESSION_TIMEOUT = 0.055
        calls = []
        def call(*args):
            method, timeout, cancel = args[3], args[-2], args[-1]
            calls.append(timeout)
            if cancel.event.wait(min(0.02, timeout / 1000)) or timeout < 20:
                raise TimeoutError("expired")
            if method == "ListSessions":
                value = ([(str(i), os.getuid(), "synthetic", "seat0", f"/session/{i}")
                          for i in range(20)],)
            else:
                value = ({"Active": False, "Remote": True, "Type": "tty",
                          "Class": "user", "LockedHint": False},)
            return SimpleNamespace(unpack=lambda: value)
        client.bus.call_sync = call
        before = time.monotonic()
        assert await client.session_active() is False
        assert time.monotonic() - before < 0.12
        assert 1 < len(calls) <= 3
        assert calls[-1] < calls[0]
        await client.close()
        assert client.bus.closed
    asyncio.run(scenario())


def test_cancelled_session_check_does_not_allocate_another_thread(fake_gi):
    async def scenario():
        client = client_module.NetworkClient()
        entered = threading.Event()
        calls = []
        def call(*args):
            calls.append(1)
            entered.set()
            assert args[-1].event.wait(1)
            time.sleep(0.03)  # Cancellation acknowledgment is deliberately late.
            raise TimeoutError("cancelled")
        client.bus.call_sync = call
        first = asyncio.create_task(client.session_active())
        while not entered.is_set():
            await asyncio.sleep(0.001)
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert await client.session_active() is False
        await client.close()
        assert calls == [1] and client._session_task is None
    asyncio.run(scenario())


def test_registration_failure_closes_initialized_network(tmp_path, monkeypatch, fake_gi):
    dependencies(monkeypatch)
    def failed(*args): raise RuntimeError("registration failed")
    monkeypatch.setattr(Bus, "register_object", failed)
    service = object.__new__(service_module.SessionService)
    with pytest.raises(RuntimeError, match="registration failed"):
        service.__init__(args(tmp_path))
    assert not service.thread.is_alive()
    assert service.network.closed and service.controller.closed
    assert all(bus.closed for bus in fake_gi.buses)


def test_name_loss_before_acquisition_never_starts(tmp_path, monkeypatch, fake_gi):
    dependencies(monkeypatch)
    service = service_module.SessionService(args(tmp_path))
    fake_gi.callbacks["lost"](service.bus, service_module.NAME)
    fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
    service.thread.join(2)
    service._dispose_bus()
    assert not service.thread.is_alive()
    assert service.controller.starts == 0 and service.network.closed


def test_slow_health_is_single_owned_poll_and_shutdown_joins_it(monkeypatch):
    async def scenario():
        service = bare_service(monkeypatch)
        active = maximum = 0
        async def sample(self):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.02)
                active -= 1
        monkeypatch.setattr(service_module.SessionService, "_sample_health", sample)
        task = asyncio.create_task(service.watch_state())
        await asyncio.sleep(0.3)
        assert service.controller.ticks >= 10
        assert not service.controller.session_unlocked
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert maximum == 1 and active == 0
    asyncio.run(scenario())


def test_explicit_stop_while_locked_prevents_unlock_resume(monkeypatch):
    async def scenario():
        service = bare_service(monkeypatch)
        active = True
        async def session_active(): return active
        async def healthy(self): return dict(HEALTHY)
        service.controller.network.session_active = session_active
        monkeypatch.setattr(service_module.SessionService, "_sample_health", healthy)
        task = asyncio.create_task(service.watch_state())
        try:
            await asyncio.sleep(0.06)
            service.controller.desired = True
            active = False
            await asyncio.sleep(0.08)
            assert service._resume_after_session
            await service.dispatch("Command", ("stop", "{}"))
            active = True
            await asyncio.sleep(0.08)
            assert service.controller.starts == 0
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_late_cancelled_bus_construction_closes_returned_connection(fake_gi):
    bus = Bus()
    cancel = Cancellable()
    def connection(*args):
        cancel.cancel()
        return bus
    fake_gi.Gio.DBusConnection.new_for_address_sync = connection
    with pytest.raises(TimeoutError):
        client_module.open_bus(fake_gi.Gio, fake_gi.Gio.BusType.SYSTEM, 1, cancel)
    assert bus.closed


def test_invalid_stop_does_not_clear_pending_resume(tmp_path, monkeypatch):
    from panelbridge.controller import Controller as RealController
    from panelbridge.state import StateStore
    async def scenario():
        service = bare_service(monkeypatch)
        service.controller = RealController(StateStore(tmp_path / "state.json"), Network(), None)
        service._autostart_pending = service._resume_after_session = True
        result = await service.dispatch("Command", ("stop", '{"extra":true}'))
        assert not result["ok"]
        assert service._autostart_pending and service._resume_after_session
    asyncio.run(scenario())


AUTOSTART_PEER = {"id": "first-run", "name": "EBPSI-TEST",
                  "peer_address": "02:00:00:00:00:01", "transport": "wireless"}


class AutostartNetwork(Network):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.active = True
        self.peers = [AUTOSTART_PEER.copy()]
        self.discoveries = 0
        self.connections = []

    async def session_active(self):
        self.poll_calls += 1
        return self.active

    async def discover(self):
        self.discoveries += 1
        return {"candidates": self.peers}

    async def connect(self, candidate_id):
        self.connections.append(candidate_id)
        return {"peer_name": AUTOSTART_PEER["name"]}

    async def keep_alive(self):
        return {}


class AutostartMedia:
    def __init__(self, *args, **kwargs):
        self.current = None

    async def start(self, link, profile):
        self.current = asyncio.get_running_loop().create_future()

    async def ready(self):
        return True

    async def wait(self):
        await self.current

    async def close(self):
        if self.current is not None and not self.current.done():
            self.current.cancel()


@pytest.fixture
def autostart_runtime(monkeypatch, fake_gi):
    from panelbridge.controller import Controller as RealController
    health = {"sample": dict(HEALTHY)}
    async def sample(self):
        return dict(health["sample"])
    monkeypatch.setattr(service_module, "Controller", lambda store, network, media:
                        RealController(store, network, media, retry_delays=(.001, .001)))
    monkeypatch.setattr(service_module, "NetworkClient", AutostartNetwork)
    monkeypatch.setattr(service_module, "DesktopMedia", AutostartMedia)
    monkeypatch.setattr(service_module, "OutputAdapter", lambda: None)
    monkeypatch.setattr(service_module.SessionService, "_sample_health", sample)
    for name, value in (("TICK_INTERVAL", .005), ("SESSION_INTERVAL", .01),
                        ("HEALTH_INTERVAL", .01), ("SESSION_TIMEOUT", .2),
                        ("HEALTH_TIMEOUT", .2)):
        monkeypatch.setattr(service_module.SessionService, name, value)
    return health


def wait_for_session(predicate):
    until = time.monotonic() + 2
    while not predicate() and time.monotonic() < until:
        time.sleep(.002)
    assert predicate(), "Session behavior did not settle"


def session_command(service, verb):
    return asyncio.run_coroutine_threadsafe(
        service.dispatch("Command", (verb, "{}")), service.async_loop).result(timeout=2)


@pytest.mark.parametrize("saved", [False, True], ids=["first_run", "saved_selection"])
def test_autostart_connects_and_persists_receiver_once(tmp_path, fake_gi, autostart_runtime, saved):
    from panelbridge.state import StateStore
    options = args(tmp_path)
    if saved:
        StateStore(options.state).select_device(AUTOSTART_PEER["peer_address"], AUTOSTART_PEER["name"])
    service = service_module.SessionService(options)
    try:
        if saved:
            service.network.peers.append({**AUTOSTART_PEER, "id": "other",
                                          "peer_address": "02:00:00:00:00:02"})
        time.sleep(.03)
        assert service.network.discoveries == 0
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.status == "streaming")
        assert StateStore(options.state).selected_device["address"] == AUTOSTART_PEER["peer_address"]
        assert service.network.connections == ["first-run"]
        assert session_command(service, "stop")["ok"]
        wait_for_session(lambda: service.controller.status == "stopped")
        service.async_loop.call_soon_threadsafe(service._start_watch)
        time.sleep(.08)
        assert service.network.discoveries == 1
        assert not service.controller.desired
    finally:
        finish_service(service)


@pytest.mark.parametrize("saved", [False, True], ids=["first_run", "saved_selection"])
def test_no_autostart_never_discovers(tmp_path, fake_gi, autostart_runtime, saved):
    from panelbridge.state import StateStore
    options = args(tmp_path)
    options.no_autostart = True
    if saved:
        StateStore(options.state).select_device(AUTOSTART_PEER["peer_address"], AUTOSTART_PEER["name"])
    service = service_module.SessionService(options)
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.session_unlocked)
        time.sleep(.05)
        assert service.network.discoveries == 0
    finally:
        finish_service(service)


@pytest.mark.parametrize('choice', ['desktop', 'headless'])
def test_oled_desktop_only_waits_for_manual_start(tmp_path, monkeypatch, fake_gi,
                                               autostart_runtime, choice):
    monkeypatch.setattr(service_module, 'read_startup_choice', lambda: choice)
    service = service_module.SessionService(args(tmp_path))
    try:
        fake_gi.callbacks['acquired'](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.session_unlocked)
        time.sleep(.05)
        assert service.network.discoveries == 0
        assert 'startup choice' in service.controller.message
        assert session_command(service, 'start')['ok']
        wait_for_session(lambda: service.controller.status == 'streaming')
        assert service.network.connections == ['first-run']
    finally:
        finish_service(service)


def test_oled_wireless_retains_ordinary_autostart(tmp_path, monkeypatch, fake_gi,
                                                autostart_runtime):
    monkeypatch.setattr(service_module, 'read_startup_choice', lambda: 'wireless')
    service = service_module.SessionService(args(tmp_path))
    try:
        fake_gi.callbacks['acquired'](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.status == 'streaming')
        assert service.network.connections == ['first-run']
    finally:
        finish_service(service)


@pytest.mark.parametrize("guard", ["broken_config", "three_failures"])
def test_autostart_preserves_configuration_and_recovery_guard(tmp_path, fake_gi, autostart_runtime, guard):
    from panelbridge.state import StateStore
    options = args(tmp_path)
    if guard == "broken_config":
        options.state.write_text("{broken")
    else:
        store = StateStore(options.state)
        store.select_device(AUTOSTART_PEER["peer_address"], AUTOSTART_PEER["name"])
        for _ in range(3):
            store.record_failure()
    before = options.state.read_bytes()
    service = service_module.SessionService(options)
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.session_unlocked)
        time.sleep(.05)
        assert service.network.discoveries == 0
        assert not service.controller.desired
        assert options.state.read_bytes() == before
    finally:
        finish_service(service)


def test_first_run_autostart_waits_for_session_and_health(tmp_path, fake_gi, autostart_runtime):
    autostart_runtime["sample"] = dict(service_module.UNKNOWN_HEALTH)
    service = service_module.SessionService(args(tmp_path))
    service.network.active = False
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.network.poll_calls >= 2)
        assert service.network.discoveries == 0
        service.network.active = True
        wait_for_session(lambda: service._session_valid)
        assert service.network.discoveries == 0
        autostart_runtime["sample"] = dict(HEALTHY)
        wait_for_session(lambda: service.controller.status == "streaming")
        assert service.network.connections == ["first-run"]
    finally:
        finish_service(service)


def test_autostart_waits_for_ethernet_without_spending_connection_attempts(
        tmp_path, monkeypatch, fake_gi, autostart_runtime):
    monkeypatch.setattr(service_module.SessionService, "NETWORK_INTERVAL", .01, raising=False)
    service = service_module.SessionService(args(tmp_path))
    service.network.recovery_available = False
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: getattr(service, "_session_valid", False)
                         and getattr(service, "_health_valid", False))
        time.sleep(.05)
        assert service.network.discoveries == 0
        assert service.controller.store.failures == 0
        service.network.recovery_available = True
        wait_for_session(lambda: service.controller.status == "streaming")
        assert service.network.connections == ["first-run"]
    finally:
        finish_service(service)


def test_stop_cancels_start_waiting_for_ethernet(
        tmp_path, monkeypatch, fake_gi, autostart_runtime):
    monkeypatch.setattr(service_module.SessionService, "NETWORK_INTERVAL", .01, raising=False)
    service = service_module.SessionService(args(tmp_path))
    service.network.recovery_available = False
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.session_unlocked)
        assert service.network.discoveries == 0
        assert session_command(service, "stop")["ok"]
        service.network.recovery_available = True
        time.sleep(.08)
        assert service.network.discoveries == 0
        assert not service.controller.desired
    finally:
        finish_service(service)


def test_ethernet_startup_wait_has_deadline_and_preserves_manual_retry(
        tmp_path, monkeypatch, fake_gi, autostart_runtime):
    monkeypatch.setattr(service_module.SessionService, "NETWORK_INTERVAL", .01, raising=False)
    monkeypatch.setattr(service_module.SessionService, "STARTUP_NETWORK_TIMEOUT", .08, raising=False)
    service = service_module.SessionService(args(tmp_path))
    service.network.recovery_available = False
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.status == "error")
        assert "Ethernet" in service.controller.message
        assert service.network.discoveries == 0
        service.network.recovery_available = True
        time.sleep(.04)
        assert service.network.discoveries == 0
        assert session_command(service, "start")["ok"]
        wait_for_session(lambda: service.controller.status == "streaming")
    finally:
        finish_service(service)


@pytest.mark.parametrize("stop_when", ["before_watch", "awaiting_health"])
@pytest.mark.parametrize("saved", [False, True], ids=["first_run", "saved_selection"])
def test_stop_before_readiness_cancels_autostart(tmp_path, fake_gi, autostart_runtime, stop_when, saved):
    from panelbridge.state import StateStore
    options = args(tmp_path)
    if saved:
        StateStore(options.state).select_device(AUTOSTART_PEER["peer_address"], AUTOSTART_PEER["name"])
    autostart_runtime["sample"] = dict(service_module.UNKNOWN_HEALTH)
    service = service_module.SessionService(options)
    try:
        if stop_when == "before_watch":
            # The name is acquired, but its queued watch callback has not run.
            service.name_owned = True
        else:
            fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
            wait_for_session(lambda: service.network.poll_calls >= 2)
        assert session_command(service, "stop")["ok"]
        if stop_when == "before_watch":
            fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        autostart_runtime["sample"] = dict(HEALTHY)
        wait_for_session(lambda: service.controller.session_unlocked)
        time.sleep(.05)
        assert service.network.discoveries == 0
        assert not service.controller.desired
    finally:
        finish_service(service)


def test_first_run_ambiguous_receivers_exhaust_existing_retries_without_rearming(
        tmp_path, fake_gi, autostart_runtime):
    service = service_module.SessionService(args(tmp_path))
    service.network.peers.append({**AUTOSTART_PEER, "id": "other",
                                  "peer_address": "02:00:00:00:00:02"})
    try:
        fake_gi.callbacks["acquired"](service.bus, service_module.NAME)
        wait_for_session(lambda: service.controller.status == "error")
        time.sleep(.05)
        assert service.network.discoveries == 3
        assert not service.network.connections
        assert service.controller.store.selected_device is None
        assert service.controller.store.recovery_required
        assert not service.controller.desired
    finally:
        finish_service(service)


TIMED_SAMPLER = """
import sys
import time
sys.path.insert(0, sys.argv[1])
from panelbridge import session_service
from panelbridge.health import HealthMonitor
clocks = iter((1000000, 1900000, 2000000, 3500000,
               4000000, 4600000, 6000000, 7000000))
counters = iter(('cpu 10 0 10 80 0 0 0 0', 'cpu 20 0 20 100 0 0 0 0',
                 'cpu 30 0 30 180 0 0 0 0', 'cpu 60 0 60 240 0 0 0 0'))
def read(path):
    return next(counters) if path == '/proc/stat' else '45000'
def throttle():
    time.sleep(0.02)
    return 'throttled=0x0'
session_service.HealthMonitor = lambda: HealthMonitor(
    read=read, throttle=throttle, clock_ns=lambda: next(clocks))
session_service.health_sampler_main()
"""


def test_timed_sampler_preserves_producer_times_and_cpu_state_across_legacy_calls(monkeypatch):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", TIMED_SAMPLER)
        sampler = service_module.HealthSampler(timeout=2)
        try:
            first = await sampler.sample_timed()
            pid = sampler.process.pid
            assert first == {"temperature_c": 45, "cpu_percent": None, "throttled_bits": 0,
                             "monotonic_us": 1900, "cpu_interval": None}
            assert await sampler.sample() == {**HEALTHY, "cpu_percent": 50}
            third = await sampler.sample_timed()
            assert third == {**HEALTHY, "monotonic_us": 4600,
                             "cpu_interval": {"start_us": 2000, "end_us": 4000}}
            fourth = await sampler.sample_timed()
            assert fourth == {**HEALTHY, "cpu_percent": 50, "monotonic_us": 7000,
                              "cpu_interval": {"start_us": 4000, "end_us": 6000}}
            assert sampler.process.pid == pid
        finally:
            await sampler.close()
        assert not sampler.processes.children
    asyncio.run(scenario())


TIMED_HEALTH = {**HEALTHY, "monotonic_us": 500,
                "cpu_interval": {"start_us": 100, "end_us": 400}}


def reply_child(value):
    raw = json.dumps(value) if not isinstance(value, bytes) else value.decode()
    return "import sys\nfor request in sys.stdin:\n print(" + repr(raw) + ", flush=True)\n"


@pytest.mark.parametrize("changes", [
    {"monotonic_us": -1}, {"monotonic_us": True}, {"monotonic_us": 500.0},
    {"monotonic_us": 2**63}, {"monotonic_us": None}, {"extra": 1},
    {"cpu_interval": None}, {"cpu_interval": []},
    {"cpu_interval": {"start_us": 100, "end_us": 400, "extra": 0}},
    {"cpu_interval": {"start_us": -1, "end_us": 400}},
    {"cpu_interval": {"start_us": True, "end_us": 400}},
    {"cpu_interval": {"start_us": 100, "end_us": 400.0}},
    {"cpu_interval": {"start_us": 400, "end_us": 400}},
    {"cpu_interval": {"start_us": 400, "end_us": 300}},
    {"cpu_interval": {"start_us": 100, "end_us": 501}},
    {"cpu_percent": None}, {"cpu_percent": True}, {"cpu_percent": 101},
    {"temperature_c": float("nan")}, {"throttled_bits": 2**32},
])
def test_invalid_timed_reply_is_rejected_and_child_reaped(monkeypatch, changes):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", reply_child(TIMED_HEALTH | changes))
        sampler = service_module.HealthSampler(timeout=2)
        try:
            with pytest.raises(ValueError):
                await sampler.sample_timed()
            assert sampler.process is None and not sampler.processes.children
        finally:
            await sampler.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("raw", [
    b'{"temperature_c":45,"cpu_percent":20,"throttled_bits":0}',
    b'{"temperature_c":45,"cpu_percent":20,"throttled_bits":0,"monotonic_us":500,"monotonic_us":600,"cpu_interval":{"start_us":100,"end_us":400}}',
    b"x" * 5000,
], ids=["legacy_shape", "duplicate_field", "oversized"])
def test_wrong_shape_duplicate_or_oversized_timed_reply_cannot_survive_ipc(monkeypatch, raw):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", reply_child(raw))
        sampler = service_module.HealthSampler(timeout=2)
        try:
            with pytest.raises(ValueError):
                await sampler.sample_timed()
            assert not sampler.processes.children
            monkeypatch.setattr(service_module, "HEALTH_SAMPLER", reply_child(TIMED_HEALTH))
            assert await sampler.sample_timed() == TIMED_HEALTH
        finally:
            await sampler.close()
        assert not sampler.processes.children
    asyncio.run(scenario())


@pytest.mark.parametrize("end", ["timeout", "cancel"])
def test_timed_sampler_stall_or_cancellation_reaps_process_and_resets_cpu_state(monkeypatch, end):
    async def scenario():
        monkeypatch.setattr(service_module, "HEALTH_SAMPLER", "import time; time.sleep(60)")
        sampler = service_module.HealthSampler(timeout=0.05 if end == "timeout" else 2)
        try:
            if end == "timeout":
                with pytest.raises(TimeoutError):
                    await sampler.sample_timed()
            else:
                task = asyncio.create_task(sampler.sample_timed())
                while not sampler.processes.children:
                    await asyncio.sleep(0.001)
                child = sampler.processes.children[0]
                with pytest.raises(RuntimeError, match="already"):
                    await sampler.sample()
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert child.returncode is not None
            assert sampler.process is None and not sampler.processes.children
            monkeypatch.setattr(service_module, "HEALTH_SAMPLER", TIMED_SAMPLER)
            sampler.timeout = 2
            restarted = await sampler.sample_timed()
            assert restarted["cpu_interval"] is None and restarted["cpu_percent"] is None
            assert restarted["monotonic_us"] == 1900
        finally:
            await sampler.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("request_bytes", [b"timed\n", b"sample_timed", b"sample_timed extra\n", b"x" * 5000],
                         ids=["unknown", "no_newline", "extra_argument", "oversized"])
def test_sampler_child_accepts_only_exact_fixed_request_lines(request_bytes):
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-c", TIMED_SAMPLER,
         str(Path(service_module.__file__).resolve().parents[1])],
        input=request_bytes, capture_output=True, timeout=2, check=False,
    )
    assert result.returncode != 0
    assert result.stdout == b""
    assert b"Invalid sample request" in result.stderr
