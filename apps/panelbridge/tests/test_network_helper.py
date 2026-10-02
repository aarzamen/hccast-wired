"""Software-only network transaction tests; no live network operations."""
import copy
import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helper.network import Budget, GioNetworkBackend, HelperError, NetworkHelper, NM, NM_PATH, select_receiver_ip, sink_available


class Clock:
    now = 100.0
    def __call__(self):
        return self.now


class Journal:
    record = None
    def read(self):
        return copy.deepcopy(self.record)
    def write(self, record):
        self.record = copy.deepcopy(record)


class Backend:
    def __init__(self):
        self.connected = True
        self.autoconnect = False
        self.checkpoint = None
        self.active = None
        self.ethernet = True
        self.fail_restore = False
        self.fail_extend = False
        self.fail_connect = False
        self.block = None
        self.peers = [{"path": "/peer/1", "name": "EBPSI-TEST", "address": "02:00:00:00:00:01", "manufacturer": "Example", "wfd": bytes.fromhex("00000600511c44012c")}]
    def recovery_available(self, budget):
        return self.ethernet
    def snapshot(self, budget):
        if not self.ethernet:
            raise HelperError("RecoveryRequired", "Connected Ethernet required")
        return {"wifi": "/wifi", "p2p": "/p2p", "wifi_interface": "wlan0", "p2p_interface": "p2p-dev-wlan0", "wifi_uuid": "11111111-1111-4111-8111-111111111111", "wifi_state": 100, "wifi_autoconnect": False}
    def checkpoint_create(self, snapshot, budget):
        self.checkpoint = "/checkpoint/1"
        return self.checkpoint
    def checkpoint_extend(self, checkpoint, budget):
        if self.fail_extend:
            raise HelperError("CheckpointFailed", "Checkpoint expired")
    def prepare(self, snapshot, budget):
        budget.check()
        self.connected = False
    def discover(self, snapshot, budget):
        return self.peers
    def resolve_peer(self, snapshot, address, budget):
        return next((p for p in self.peers if p["address"] == address), None)
    def activate(self, snapshot, peer, connection_uuid, budget):
        if self.block:
            self.block[0].set()
            self.block[1].wait(2)
        budget.check()
        if self.fail_connect:
            raise HelperError("ConnectionFailed", "Activation failed")
        self.active = connection_uuid
        return {"peer_ip": "192.0.2.1", "group_interface": "p2p-wlan0-0"}
    def restore(self, record, budget):
        self.active = None
        self.checkpoint = None
        if self.fail_restore:
            return ["wifi-restore-failed"]
        self.connected = True
        self.autoconnect = record["snapshot"]["wifi_autoconnect"]
        return []


@pytest.fixture
def setup():
    backend, journal, clock = Backend(), Journal(), Clock()
    return NetworkHelper(backend, journal, clock=clock), backend, journal, clock


def test_discovery_connection_and_release_restore_original_values(setup):
    helper, backend, journal, clock = setup
    candidates = helper.discover(":1.1")["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["id"] != candidates[0]["peer_address"]
    assert not backend.connected
    result = helper.connect(":1.1", candidates[0]["id"])
    assert result["peer_ip"] == "192.0.2.1"
    assert result["lease_deadline"] == 160.0
    assert journal.record["group_interface"] == result["group_interface"]
    assert helper.release(":1.1")["network_restored"] is True
    assert backend.connected and backend.autoconnect is False
    assert journal.record["state"] == "restored"


@pytest.mark.parametrize("method, error", [("Discover", "Busy"), ("Connect", "StaleCandidate")])
def test_same_owner_duplicate_request_preserves_live_stream_and_lease(setup, method, error):
    helper, backend, journal, clock = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    connection = helper.connect(":1.1", candidate["id"])
    previous = copy.deepcopy(journal.record)
    with pytest.raises(HelperError, match=error):
        if method == "Discover":
            helper.discover(":1.1")
        else:
            helper.connect(":1.1", candidate["id"])
    assert backend.active == connection["connection_uuid"]
    assert backend.checkpoint == "/checkpoint/1" and not backend.connected
    assert helper.owner == ":1.1" and helper.status()["state"] == "connected"
    assert helper.status()["failed_attempts"] == 0
    assert helper.status()["last_error"] is None
    assert journal.record == previous
    clock.now = 110
    assert helper.keep_alive(":1.1")["lease_deadline"] == 170
    assert helper.release(":1.1")["network_restored"] is True


def test_previous_lease_candidate_cannot_release_or_replace_current_discovery(setup):
    helper, backend, journal, _ = setup
    expired_candidate = helper.discover(":1.1")["candidates"][0]
    helper.release(":1.1")
    current_candidate = helper.discover(":1.1")["candidates"][0]
    previous = copy.deepcopy(journal.record)
    with pytest.raises(HelperError, match="StaleCandidate"):
        helper.connect(":1.1", expired_candidate["id"])
    assert helper.status()["state"] == "discovered" and helper.owner == ":1.1"
    assert backend.checkpoint == "/checkpoint/1" and not backend.connected
    assert helper.status()["failed_attempts"] == 0 and journal.record == previous
    helper.connect(":1.1", current_candidate["id"])
    assert helper.status()["state"] == "connected"


@pytest.mark.parametrize("method", ["Discover", "Connect"])
def test_expired_live_lease_is_restored_before_rejecting_stale_request(setup, method):
    helper, backend, journal, clock = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    helper.connect(":1.1", candidate["id"])
    clock.now = 161
    with pytest.raises(HelperError, match="NoLease"):
        if method == "Discover":
            helper.discover(":1.1")
        else:
            helper.connect(":1.1", candidate["id"])
    assert backend.active is None and backend.connected
    assert helper.owner is None and helper.status()["failed_attempts"] == 0
    assert journal.record["state"] == "restored"


def test_other_owner_and_stale_selection_never_activate(setup):
    helper, backend, _, _ = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    with pytest.raises(HelperError, match="Busy"):
        helper.connect(":1.2", candidate["id"])
    backend.peers = [{**backend.peers[0], "name": "Other-brand"}]
    with pytest.raises(HelperError, match="StaleCandidate"):
        helper.connect(":1.1", candidate["id"])
    assert backend.active is None and backend.connected


def test_no_recovery_route_never_changes_wifi(setup):
    helper, backend, _, _ = setup
    backend.ethernet = False
    with pytest.raises(HelperError, match="RecoveryRequired"):
        helper.discover(":1.1")
    assert backend.connected


def test_lease_expiry_invalidates_candidate_and_restores(setup):
    helper, backend, _, clock = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    clock.now = 161
    assert helper.expire()
    assert backend.connected
    with pytest.raises(HelperError, match="NoLease"):
        helper.connect(":1.1", candidate["id"])


def test_failed_checkpoint_extension_fails_closed(setup):
    helper, backend, _, _ = setup
    helper.discover(":1.1")
    backend.fail_extend = True
    with pytest.raises(HelperError, match="CheckpointFailed"):
        helper.keep_alive(":1.1")
    assert backend.connected and not helper.status()["owner_present"]


def test_failed_restore_is_retained_and_blocks_new_work(setup):
    helper, backend, journal, _ = setup
    helper.discover(":1.1")
    backend.fail_restore = True
    assert helper.release(":1.1")["network_restored"] is False
    assert journal.record["state"] == "restore_failed"
    with pytest.raises(HelperError, match="RestoreRequired"):
        helper.discover(":1.2")
    backend.fail_restore = False
    assert helper.release(":1.2")["network_restored"] is True
    assert helper.status()["last_error"] is None


def test_status_reports_restoration_stages_until_successful_recovery(setup):
    helper, backend, journal, clock = setup
    helper.discover(":1.1")
    backend.fail_restore = True
    helper.release(":1.1")
    assert helper.status()["restoration_errors"] == ["wifi-restore-failed"]
    restarted = NetworkHelper(backend, journal, clock=clock)
    assert restarted.status()["restoration_errors"] == ["wifi-restore-failed"]
    backend.fail_restore = False
    assert restarted.recover()["network_restored"] is True
    assert restarted.status()["restoration_errors"] == []


@pytest.mark.parametrize("recorded_errors", [
    ["wifi-restore-failed", "SECRET /private/device", {"secret": "value"}, "wifi-restore-failed"],
    "SECRET /private/device",
    None,
])
def test_status_filters_private_or_malformed_journal_error_values(recorded_errors):
    backend, journal = Backend(), Journal()
    journal.record = {"state": "restore_failed", "snapshot": backend.snapshot(None), "errors": recorded_errors}
    helper = NetworkHelper(backend, journal)
    expected = ["wifi-restore-failed"] if isinstance(recorded_errors, list) else []
    assert helper.status()["restoration_errors"] == expected
    assert "SECRET" not in json.dumps(helper.status())


def test_successful_cleanup_preserves_original_connection_failure(setup):
    helper, backend, _, _ = setup
    backend.fail_connect = True
    candidate = helper.discover(":1.1")["candidates"][0]
    with pytest.raises(HelperError, match="ConnectionFailed"):
        helper.connect(":1.1", candidate["id"])
    assert helper.status()["state"] == "idle"
    assert helper.status()["last_error"] == "ConnectionFailed"


def test_sender_disappearing_cancels_inflight_connect_before_activation(setup):
    helper, backend, _, _ = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    entered, resume = threading.Event(), threading.Event()
    backend.block = entered, resume
    errors = []
    def connect():
        try:
            helper.connect(":1.1", candidate["id"])
        except HelperError as error:
            errors.append(error.code)
    thread = threading.Thread(target=connect)
    thread.start()
    assert entered.wait(2)
    assert helper.cancel_owner(":1.1")
    resume.set()
    thread.join(2)
    assert not thread.is_alive()
    assert errors == ["Cancelled"]
    assert backend.active is None and backend.connected


def test_restart_restores_incomplete_transaction(setup):
    helper, backend, journal, clock = setup
    helper.discover(":1.1")
    restarted = NetworkHelper(backend, journal, clock=clock)
    assert restarted.recover()["network_restored"] is True
    assert backend.connected and journal.record["state"] == "restored"


@pytest.mark.parametrize("ies, expected", [
    ("00000600511c44012c", True), ("00000600531c44012c", True),
    ("00000600901c4400c8", False), ("00000600411c44012c", False),
    ("00000600511c44", False), ("ff", False), ("", False),
])
def test_wfd_accepts_only_available_sink_advertisements(ies, expected):
    assert sink_available(bytes.fromhex(ies)) is expected


def test_client_and_group_owner_ip_selection():
    addresses = [{"address": "192.0.2.10", "prefix": 24}]
    assert select_receiver_ip("192.0.2.1", addresses, [], "", 100) == "192.0.2.1"
    leases = "200 02:00:00:00:00:01 192.0.2.2 receiver *\n"
    assert select_receiver_ip("", addresses, ["02:00:00:00:00:01"], leases, 100) == "192.0.2.2"


@pytest.mark.parametrize("gateway,stations,leases", [
    ("198.51.100.1", [], ""), ("192.0.2.10", [], ""),
    ("192.0.2.255", [], ""), ("", ["02:00:00:00:00:01"], "99 02:00:00:00:00:01 192.0.2.2 receiver *"),
    ("", ["02:00:00:00:00:01", "02:00:00:00:00:02"], ""),
])
def test_wrong_subnet_own_ip_broadcast_stale_and_ambiguous_lease_fail(gateway, stations, leases):
    with pytest.raises(HelperError):
        select_receiver_ip(gateway, [{"address": "192.0.2.10", "prefix": 24}], stations, leases, 100)


def test_budget_caps_each_blocking_call_and_checks_cancellation():
    clock, cancel = Clock(), threading.Event()
    budget = Budget(120, cancel, clock=clock)
    assert budget.timeout_ms() == 5000
    clock.now = 119.5
    assert budget.timeout_ms() == 500
    cancel.set()
    with pytest.raises(HelperError, match="Cancelled"):
        budget.timeout_ms()


class Variant:
    def __init__(self, signature, value):
        self.signature, self.value = signature, value


class AdvancingBudget(Budget):
    """Advance deterministic time on a poll; never sleep or touch real devices."""
    def __init__(self, seconds=5, on_pause=None):
        self.test_clock = Clock()
        self.on_pause = on_pause
        super().__init__(self.test_clock() + seconds, clock=self.test_clock)
    def pause(self, seconds=0.2):
        self.test_clock.now += seconds
        if self.on_pause:
            self.on_pause()
        self.check()


class NMFixture(GioNetworkBackend):
    """Stateful bus boundary: runs the real discovery/activation/restoration code."""
    def __init__(self):
        self.GLib = type("GLib", (), {"Variant": Variant})
        self.activation_uncertain = False
        self.fail_method = None
        self.calls = []
        self.commands = []
        self.links = set()
        self.defaults = [{"dst": "default", "gateway": "198.51.100.1", "dev": "eth0", "metric": 100}]
        self.data = {
            (NM_PATH, NM): {"ActiveConnections": ["/active/home"], "Checkpoints": ["/checkpoint/1"]},
            ("/wifi", NM + ".Device"): {"DeviceType": 2, "Interface": "wlan0", "Managed": True, "State": 100,
                                       "Autoconnect": False, "ActiveConnection": "/active/home"},
            ("/p2p", NM + ".Device"): {"DeviceType": 30, "Interface": "p2p-dev-wlan0", "Managed": True,
                                       "State": 30, "ActiveConnection": "/", "IpInterface": "", "Ip4Config": "/ip4"},
            ("/eth", NM + ".Device"): {"DeviceType": 1, "Interface": "eth0", "IpInterface": "eth0", "State": 100, "Ip4Config": "/ether-ip4"},
            ("/active/home", NM + ".Connection.Active"): {"Uuid": "11111111-1111-4111-8111-111111111111", "Type": "802-11-wireless"},
            ("/active/p2p", NM + ".Connection.Active"): {"Uuid": "22222222-2222-4222-8222-222222222222", "Type": "wifi-p2p", "State": 2},
            ("/ip4", NM + ".IP4Config"): {"Gateway": "192.0.2.1", "AddressData": [{"address": "192.0.2.10", "prefix": 24}]},
        }
    def props(self, path, interface, budget):
        budget.check()
        return copy.deepcopy(self.data[path, interface])
    def devices(self, budget):
        return [(p, self.props(p, NM + ".Device", budget)) for p in ("/wifi", "/p2p", "/eth")]
    def routes(self, budget):
        return copy.deepcopy(self.defaults)
    def command(self, args, budget):
        budget.check()
        self.commands.append(args)
        if args[:4] == ["/usr/sbin/ip", "-j", "route", "get"]:
            return '[{"dev":"p2p-wlan0-0"}]'
        if args == ["/usr/sbin/ip", "-j", "link", "show"]:
            return json.dumps([{"ifname": name} for name in sorted(self.links)])
        raise AssertionError(args)
    def call(self, path, interface, method, budget, signature=None, args=None):
        budget.check()
        self.calls.append((method, args))
        if method == self.fail_method:
            raise HelperError("NetworkFailure", "Injected bus failure")
        if method == "AddAndActivateConnection2":
            self.data["/p2p", NM + ".Device"].update(State=100, ActiveConnection="/active/p2p", IpInterface="p2p-wlan0-0")
            self.links.add("p2p-wlan0-0")
            self.data[NM_PATH, NM]["ActiveConnections"].append("/active/p2p")
            return "/settings/p2p", "/active/p2p", {}
        if method == "DeactivateConnection":
            self.data["/p2p", NM + ".Device"].update(State=30, ActiveConnection="/", IpInterface="")
            self.links.discard("p2p-wlan0-0")
            self.data[NM_PATH, NM]["ActiveConnections"].remove(args[0])
        if method == "CheckpointRollback":
            self.data["/wifi", NM + ".Device"].update(State=100, ActiveConnection="/active/home", Autoconnect=False)
            self.data[NM_PATH, NM]["Checkpoints"] = []
            return ({"/wifi": 0},)
        if method == "CheckpointDestroy":
            self.data[NM_PATH, NM]["Checkpoints"].remove(args[0])
        if method == "GetConnectionByUuid":
            return ("/settings/home",)
        if method == "ActivateConnection":
            self.data["/wifi", NM + ".Device"].update(State=100, ActiveConnection="/active/home")
            return ("/active/home",)
        if method == "Set":
            self.data[path, args[0]][args[1]] = args[2].value
        return ()


def test_nm_discovers_interfaces_by_type_and_rejects_wifi_default_route():
    backend = NMFixture()
    budget = Budget(1000, clock=Clock())
    snapshot = backend.snapshot(budget)
    assert snapshot["wifi_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert snapshot["wifi_autoconnect"] is False
    backend.defaults[0]["dev"] = "wlan0"
    with pytest.raises(HelperError, match="RecoveryRequired"):
        backend.snapshot(budget)


class RecoveryBoundaryFixture(NMFixture):
    """Keep the real device/route readers; replace only D-Bus and command I/O."""
    devices = GioNetworkBackend.devices
    routes = GioNetworkBackend.routes

    def call(self, path, interface, method, budget, signature=None, args=None):
        if method in ("GetDevices", "GetAll"):
            budget.check()
            self.calls.append((method, args))
            if method == "GetDevices":
                return (["/wifi", "/p2p", "/eth"],)
            return (copy.deepcopy(self.data[path, args[0]]),)
        return super().call(path, interface, method, budget, signature, args)

    props = GioNetworkBackend.props

    def command(self, args, budget):
        if args == ["/usr/sbin/ip", "-j", "route", "show", "default"]:
            budget.check()
            self.commands.append(args)
            return json.dumps(self.defaults)
        return super().command(args, budget)


@pytest.mark.parametrize("routes, ready", [
    ([], False),
    ([{"dev": "eth0", "metric": 100}], True),
    ([{"dev": "eth0", "metric": 100}, {"dev": "wlan0", "metric": 50}], False),
    ([{"dev": "wlan0", "metric": 200}, {"dev": "eth0", "metric": 100}], True),
    ([{"dev": "wlan0"}, {"dev": "eth0", "metric": 100}], False),
    ([{"dev": "eth0"}, {"dev": "wlan0", "metric": 100}], True),
    ([{"dev": "wlan0", "metric": 100}, {"dev": "eth0", "metric": 100}], False),
    ([{"dev": "eth0", "metric": 100}, {"dev": "wlan0", "metric": 100}], True),
])
def test_status_recovery_requires_the_same_preferred_ethernet_route_as_discovery(routes, ready):
    backend, journal = RecoveryBoundaryFixture(), Journal()
    backend.defaults = routes
    original = copy.deepcopy(backend.data)
    helper = NetworkHelper(backend, journal, clock=Clock())
    assert helper.status(refresh=True)["recovery_available"] is ready
    assert backend.calls == [("GetDevices", None)] + [("GetAll", (NM + ".Device",))] * 3
    assert backend.commands == [["/usr/sbin/ip", "-j", "route", "show", "default"]]
    assert backend.data == original and journal.record is None
    if ready:
        assert backend.snapshot(AdvancingBudget())["default_routes"]
    else:
        with pytest.raises(HelperError, match="RecoveryRequired"):
            backend.snapshot(AdvancingBudget())


@pytest.mark.parametrize("change", [{"State": 30}, {"Ip4Config": "/"}])
def test_status_recovery_requires_active_ethernet_with_ip_configuration(change):
    backend = RecoveryBoundaryFixture()
    backend.data["/eth", NM + ".Device"].update(change)
    helper = NetworkHelper(backend, Journal(), clock=Clock())
    assert helper.status(refresh=True)["recovery_available"] is False
    assert not backend.commands


def test_status_route_inspection_respects_the_existing_budget(monkeypatch):
    backend = RecoveryBoundaryFixture()
    budget = AdvancingBudget(seconds=5)
    helper = NetworkHelper(backend, Journal(), clock=budget.clock)
    monkeypatch.setattr(helper, "_budget", lambda seconds, **kwargs: budget)
    command = backend.command
    def expired_command(args, remaining):
        budget.test_clock.now = budget.deadline
        return command(args, remaining)
    backend.command = expired_command
    assert helper.status(refresh=True)["recovery_available"] is False
    assert budget.clock() == budget.deadline
    assert not backend.commands


def test_startfind_constructs_signed_int32_timeout_within_operation_budget():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=20)
    backend._peers = lambda snapshot, remaining: Backend().peers
    peers = backend.discover({"p2p": "/p2p"}, budget)
    assert peers[0]["name"] == "EBPSI-TEST"
    timeout = backend.calls[0][1][0]["timeout"]
    assert timeout.signature == "i"
    assert timeout.value == 20
    assert backend.calls[-1][0] == "StopFind"


@pytest.mark.parametrize("autoconnect", [False, True])
def test_prepare_already_idle_wifi_preserves_ethernet_only_baseline(autoconnect):
    backend = NMFixture()
    budget = AdvancingBudget(seconds=20)
    backend.data["/wifi", NM + ".Device"].update(
        State=30, ActiveConnection="/", Autoconnect=autoconnect)
    snapshot = backend.snapshot(budget)
    # Real NM rejects Disconnect with Device.NotActive in this state.
    backend.fail_method = "Disconnect"
    backend.prepare(snapshot, budget)
    assert not any(method == "Disconnect" for method, _ in backend.calls)
    assert backend.data["/wifi", NM + ".Device"]["Autoconnect"] is False
    assert snapshot["wifi_state"] == 30 and snapshot["wifi_uuid"] is None
    assert snapshot["wifi_autoconnect"] is autoconnect


def test_prepare_connected_wifi_still_disconnects_for_p2p():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=20)
    backend.prepare(backend.snapshot(budget), budget)
    assert sum(method == "Disconnect" for method, _ in backend.calls) == 1


def test_nm_activation_is_volatile_bound_to_helper_and_never_default():
    backend = NMFixture()
    budget = Budget(1000, clock=Clock())
    snapshot = backend.snapshot(budget)
    result = backend.activate(snapshot, Backend().peers[0], "22222222-2222-4222-8222-222222222222", budget)
    assert result == {"peer_ip": "192.0.2.1", "group_interface": "p2p-wlan0-0"}
    method, args = backend.calls[0]
    assert method == "AddAndActivateConnection2"
    assert args[3]["persist"].value == "volatile"
    assert args[3]["bind-activation"].value == "dbus-client"
    assert args[0]["ipv4"]["never-default"].value is True
    assert args[0]["ipv6"]["never-default"].value is True
    assert args[1:3] == ("/p2p", "/peer/1")


def test_nm_restore_continues_after_stopfind_failure_and_preserves_error():
    backend = NMFixture()
    budget = Budget(1000, clock=Clock())
    snapshot = backend.snapshot(budget)
    backend.activate(snapshot, Backend().peers[0], "22222222-2222-4222-8222-222222222222", budget)
    backend.data["/wifi", NM + ".Device"].update(State=30, ActiveConnection="/", Autoconnect=False)
    backend.fail_method = "StopFind"
    errors = backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1", "connection_uuid": "22222222-2222-4222-8222-222222222222"}, budget)
    assert errors == ["discovery-stop-failed"]
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100
    assert backend.data["/p2p", NM + ".Device"]["ActiveConnection"] == "/"


def test_restore_waits_read_only_for_recorded_ethernet_route_to_appear():
    backend = NMFixture()
    snapshot = backend.snapshot(AdvancingBudget())
    original_devices = copy.deepcopy(backend.data)
    original_routes = copy.deepcopy(backend.defaults)
    backend.defaults = []
    polls = []
    def publish_route():
        polls.append(tuple(backend.calls))
        if len(polls) == 3:
            backend.defaults = original_routes
    budget = AdvancingBudget(seconds=35, on_pause=publish_route)
    assert backend.restore({"snapshot": snapshot}, budget) == []
    assert len(polls) == 3
    # Discovery cleanup precedes the wait; no mutation is retried while waiting.
    assert all(calls == (("StopFind", None),) for calls in polls)
    assert backend.calls == [("StopFind", None)]
    assert backend.data == original_devices
    assert backend.defaults == original_routes


def test_restore_missing_ethernet_route_exhausts_existing_deadline():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=1)
    snapshot = backend.snapshot(budget)
    backend.defaults = []
    assert backend.restore({"snapshot": snapshot}, budget) == ["network-verification-failed"]
    assert budget.clock() == pytest.approx(budget.deadline)
    assert backend.calls == [("StopFind", None)]


def test_restore_does_not_wait_for_routes_after_an_earlier_cleanup_error():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    snapshot = backend.snapshot(budget)
    backend.defaults = []
    backend.fail_method = "StopFind"
    assert backend.restore({"snapshot": snapshot}, budget) == [
        "discovery-stop-failed", "network-verification-failed"]
    assert budget.clock() == 100


def test_restore_does_not_wait_for_routes_with_a_foreign_p2p_connection():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    snapshot = backend.snapshot(budget)
    backend.defaults = []
    backend.data["/p2p", NM + ".Device"].update(State=100, ActiveConnection="/active/p2p")
    assert backend.restore({"snapshot": snapshot}, budget) == [
        "p2p-drain-conflict", "network-verification-failed"]
    assert budget.clock() == 100
    assert not any(method == "DeactivateConnection" for method, _ in backend.calls)


def test_restore_route_wait_stops_when_a_foreign_p2p_connection_appears():
    backend = NMFixture()
    snapshot = backend.snapshot(AdvancingBudget())
    backend.defaults = []
    def foreign_p2p():
        backend.data["/p2p", NM + ".Device"].update(State=100, ActiveConnection="/active/p2p")
    budget = AdvancingBudget(seconds=35, on_pause=foreign_p2p)
    assert backend.restore({"snapshot": snapshot}, budget) == ["network-verification-failed"]
    assert budget.clock() == pytest.approx(100.2)
    assert not any(method == "DeactivateConnection" for method, _ in backend.calls)


def test_restore_route_wait_stops_when_a_foreign_wifi_connection_appears():
    backend = NMFixture()
    snapshot = backend.snapshot(AdvancingBudget())
    backend.defaults = []
    def foreign_wifi():
        backend.data["/active/foreign", NM + ".Connection.Active"] = {
            "Uuid": "33333333-3333-4333-8333-333333333333", "Type": "802-11-wireless"}
        backend.data["/wifi", NM + ".Device"]["ActiveConnection"] = "/active/foreign"
        backend.defaults = snapshot["default_routes"]
    budget = AdvancingBudget(seconds=35, on_pause=foreign_wifi)
    assert backend.restore({"snapshot": snapshot}, budget) == ["network-verification-failed"]
    assert budget.clock() == pytest.approx(100.2)
    assert backend.calls == [("StopFind", None)]
    assert backend.data["/wifi", NM + ".Device"]["ActiveConnection"] == "/active/foreign"


class DelayedP2PFixture(NMFixture):
    def __init__(self):
        super().__init__()
        self.polls = 0
        self.stagnant = False
        self.rollback_views = []
    def call(self, path, interface, method, budget, signature=None, args=None):
        if method == "CheckpointRollback":
            self.rollback_views.append((copy.deepcopy(self.data["/p2p", NM + ".Device"]), set(self.links)))
        result = super().call(path, interface, method, budget, signature, args)
        if method == "DeactivateConnection":
            self.data["/p2p", NM + ".Device"].update(State=110, IpInterface="p2p-wlan0-0")
            self.links.add("p2p-wlan0-0")
        return result
    def advance(self):
        self.polls += 1
        if not self.stagnant:
            self.data["/p2p", NM + ".Device"].update(State=30, IpInterface="")
            if self.polls >= 2:
                self.links.discard("p2p-wlan0-0")


def connected_record(backend, budget):
    snapshot = backend.snapshot(budget)
    connection_uuid = "22222222-2222-4222-8222-222222222222"
    backend.activate(snapshot, Backend().peers[0], connection_uuid, budget)
    backend.data["/wifi", NM + ".Device"].update(State=30, ActiveConnection="/")
    return {"snapshot": snapshot, "checkpoint": "/checkpoint/1", "connection_uuid": connection_uuid}


def test_restore_waits_for_nm_and_exact_group_netdev_before_rollback():
    backend = DelayedP2PFixture()
    budget = AdvancingBudget(seconds=35, on_pause=backend.advance)
    record = connected_record(backend, budget)
    assert backend.restore(record, budget) == []
    device, links = backend.rollback_views[0]
    assert device["State"] == 30 and device["ActiveConnection"] == "/" and device["IpInterface"] == ""
    assert "p2p-wlan0-0" not in links
    assert backend.polls == 2
    assert ["/usr/sbin/ip", "-j", "link", "show"] in backend.commands


@pytest.mark.parametrize("seconds, elapsed", [(35, 5), (4, 1)])
def test_drain_timeout_preserves_home_restore_budget_and_failure_journal(monkeypatch, seconds, elapsed):
    backend, journal = DelayedP2PFixture(), Journal()
    backend.stagnant = True
    budget = AdvancingBudget(seconds=seconds, on_pause=backend.advance)
    journal.record = {**connected_record(backend, budget), "state": "connected"}
    helper = NetworkHelper(backend, journal, clock=budget.clock)
    monkeypatch.setattr(helper, "_budget", lambda seconds, **kwargs: budget)
    result = helper.recover()
    assert result["errors"] == ["p2p-drain-timed-out"]
    assert not result["network_restored"]
    assert helper.state == "restore_required" and journal.record["state"] == "restore_failed"
    assert journal.record["group_interface"] == "p2p-wlan0-0"
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100
    assert 100 + elapsed - 0.1 <= budget.clock() <= 100 + elapsed + 0.1
    assert any(method == "CheckpointRollback" for method, _ in backend.calls)
    # A restarted helper must retain the exact group check even when NM has
    # forgotten the connection but its old netdev remains.
    backend.data["/p2p", NM + ".Device"].update(State=30, ActiveConnection="/", IpInterface="")
    retry_budget = AdvancingBudget(seconds=4)
    restarted = NetworkHelper(backend, journal, clock=retry_budget.clock)
    monkeypatch.setattr(restarted, "_budget", lambda seconds, **kwargs: retry_budget)
    assert restarted.recover()["errors"] == ["p2p-drain-timed-out"]
    backend.links.clear()
    assert restarted.recover()["network_restored"] is True
    assert journal.record["state"] == "restored" and restarted.state == "idle"


@pytest.mark.parametrize("boundary", ["props", "command"])
def test_drain_boundary_timeout_gets_specific_error_and_leaves_home_budget(boundary):
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    record = connected_record(backend, budget)
    original_props, original_command = backend.props, backend.command
    def props(path, interface, remaining):
        if boundary == "props" and path == "/p2p" and remaining is not budget:
            budget.test_clock.now = remaining.deadline
            raise HelperError("NetworkFailure", "Bounded D-Bus call timed out")
        return original_props(path, interface, remaining)
    def command(args, remaining):
        if boundary == "command" and args == ["/usr/sbin/ip", "-j", "link", "show"]:
            budget.test_clock.now = remaining.deadline
            raise HelperError("InspectionFailed", "Bounded command timed out")
        return original_command(args, remaining)
    backend.props, backend.command = props, command
    errors = backend.restore(record, budget)
    assert errors == ["p2p-drain-timed-out"]
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100
    assert budget.clock() == 105


def test_recorded_group_is_checked_when_nm_already_forgot_the_active_connection():
    backend = DelayedP2PFixture()
    budget = AdvancingBudget(seconds=35, on_pause=backend.advance)
    record = connected_record(backend, budget)
    record["group_interface"] = "p2p-wlan0-0"
    backend.data["/p2p", NM + ".Device"].update(State=30, ActiveConnection="/", IpInterface="")
    backend.data[NM_PATH, NM]["ActiveConnections"] = ["/active/home"]
    assert backend.restore(record, budget) == []
    assert "p2p-wlan0-0" not in backend.rollback_views[0][1]
    assert not any(method == "DeactivateConnection" for method, _ in backend.calls)


def test_drain_never_deactivates_a_foreign_p2p_connection():
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    record = connected_record(backend, budget)
    backend.data["/active/p2p", NM + ".Connection.Active"]["Uuid"] = "33333333-3333-4333-8333-333333333333"
    errors = backend.restore(record, budget)
    assert "p2p-drain-conflict" in errors
    assert not any(method == "DeactivateConnection" for method, _ in backend.calls)
    assert backend.data["/p2p", NM + ".Device"]["ActiveConnection"] == "/active/p2p"


@pytest.mark.parametrize("group", ["eth0", "p2p-wlan1-0", "../../wlan0"])
def test_drain_rejects_unbound_recorded_group_without_constructing_commands(group):
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    record = connected_record(backend, budget)
    record["group_interface"] = group
    errors = backend.restore(record, budget)
    assert "p2p-drain-failed" in errors
    assert ["/usr/sbin/ip", "-j", "link", "show"] not in backend.commands
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100


@pytest.mark.parametrize("raw", ['{}', '[{}]', '[{"ifname":"../invalid"}]'])
def test_drain_rejects_malformed_link_inspection_and_still_restores_home(raw):
    backend = NMFixture()
    budget = AdvancingBudget(seconds=35)
    record = connected_record(backend, budget)
    original = backend.command
    backend.command = lambda args, remaining: raw if args == ["/usr/sbin/ip", "-j", "link", "show"] else original(args, remaining)
    errors = backend.restore(record, budget)
    assert "p2p-drain-failed" in errors
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100


class DeactivatingNMFixture(NMFixture):
    def call(self, path, interface, method, budget, signature=None, args=None):
        result = super().call(path, interface, method, budget, signature, args)
        if method == "CheckpointRollback":
            self.data["/wifi", NM + ".Device"].update(State=110, ActiveConnection="/active/home", Autoconnect=False)
        return result


def test_restore_reactivates_original_after_checkpoint_returns_while_deactivating():
    backend = DeactivatingNMFixture()
    def finish_disconnect():
        if backend.data["/wifi", NM + ".Device"]["State"] == 110:
            backend.data["/wifi", NM + ".Device"].update(State=30, ActiveConnection="/")
    budget = AdvancingBudget(on_pause=finish_disconnect)
    snapshot = backend.snapshot(budget)
    errors = backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget)
    assert errors == []
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100
    assert ("GetConnectionByUuid", ("11111111-1111-4111-8111-111111111111",)) in backend.calls
    assert sum(method == "ActivateConnection" for method, _ in backend.calls) == 1


def test_restore_never_overwrites_foreign_association_appearing_during_transition():
    backend = DeactivatingNMFixture()
    def foreign_connection():
        backend.data["/active/foreign", NM + ".Connection.Active"] = {"Uuid": "33333333-3333-4333-8333-333333333333"}
        backend.data["/wifi", NM + ".Device"].update(State=100, ActiveConnection="/active/foreign")
    budget = AdvancingBudget(on_pause=foreign_connection)
    snapshot = backend.snapshot(budget)
    errors = backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget)
    assert "wifi-restore-failed" in errors
    assert not any(method in ("Set", "ActivateConnection") for method, _ in backend.calls)
    assert backend.data["/wifi", NM + ".Device"]["ActiveConnection"] == "/active/foreign"


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_late_foreign_conflict_cancels_surviving_checkpoint_and_journals_failure(monkeypatch, cancel_fails):
    backend, journal = NMFixture(), Journal()
    snapshot = backend.snapshot(AdvancingBudget())
    journal.record = {"api_version": 1, "state": "preparing", "snapshot": snapshot, "checkpoint": "/checkpoint/1"}
    backend.data["/wifi", NM + ".Device"].update(State=110, ActiveConnection="/active/home")
    backend.fail_method = "CheckpointRollback"
    def foreign_connection():
        backend.data["/active/foreign", NM + ".Connection.Active"] = {"Uuid": "33333333-3333-4333-8333-333333333333"}
        backend.data["/wifi", NM + ".Device"].update(State=100, ActiveConnection="/active/foreign")
        backend.fail_method = "CheckpointDestroy" if cancel_fails else None
    helper = NetworkHelper(backend, journal)
    monkeypatch.setattr(helper, "_budget", lambda seconds, **kwargs: AdvancingBudget(on_pause=foreign_connection))
    result = helper.recover()
    assert ("CheckpointDestroy", ("/checkpoint/1",)) in backend.calls
    assert backend.data[NM_PATH, NM]["Checkpoints"] == (["/checkpoint/1"] if cancel_fails else [])
    assert result["network_restored"] is False
    assert "checkpoint-rollback-failed" in result["errors"]
    assert ("checkpoint-cancel-failed" in result["errors"]) is cancel_fails
    assert journal.record["state"] == "restore_failed" and journal.record["errors"] == result["errors"]
    assert backend.data["/wifi", NM + ".Device"]["ActiveConnection"] == "/active/foreign"
    assert not any(method in ("Set", "ActivateConnection") for method, _ in backend.calls)


def test_restore_stagnant_deactivation_exhausts_finite_budget_without_activation():
    backend = DeactivatingNMFixture()
    budget = AdvancingBudget(seconds=1)
    snapshot = backend.snapshot(budget)
    errors = backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget)
    assert "wifi-restore-failed" in errors
    assert not any(method == "ActivateConnection" for method, _ in backend.calls)
    assert budget.test_clock() < 102


def test_restore_tolerates_old_active_object_disappearing_between_property_reads():
    class ReapedActiveFixture(DeactivatingNMFixture):
        def props(self, path, interface, budget):
            if path == "/active/home" and self.data["/wifi", NM + ".Device"]["State"] == 110:
                self.data["/wifi", NM + ".Device"].update(State=30, ActiveConnection="/")
                raise HelperError("NetworkFailure", "Old active object disappeared")
            return super().props(path, interface, budget)
    backend = ReapedActiveFixture()
    budget = AdvancingBudget()
    snapshot = backend.snapshot(budget)
    assert backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget) == []
    assert backend.data["/wifi", NM + ".Device"]["State"] == 100


def test_restore_waits_for_activation_to_take_effect_without_repeating_activation():
    class DeferredActivationFixture(NMFixture):
        pending = False
        polls = 0
        def call(self, path, interface, method, budget, signature=None, args=None):
            result = super().call(path, interface, method, budget, signature, args)
            if method in ("CheckpointRollback", "ActivateConnection"):
                self.data["/wifi", NM + ".Device"].update(State=30, ActiveConnection="/")
            if method == "ActivateConnection":
                self.pending = True
            return result
        def advance(self):
            if self.pending:
                self.polls += 1
                if self.polls == 3:
                    self.data["/wifi", NM + ".Device"].update(State=100, ActiveConnection="/active/home")
    backend = DeferredActivationFixture()
    budget = AdvancingBudget(on_pause=backend.advance)
    snapshot = backend.snapshot(budget)
    assert backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget) == []
    assert sum(method == "ActivateConnection" for method, _ in backend.calls) == 1


@pytest.mark.parametrize("remote_name, expected", [
    ("org.freedesktop.NetworkManager.Device.InvalidArgument", "org.freedesktop.NetworkManager.Device.InvalidArgument"),
    ("org.freedesktop.DBus.Error.NoReply", "org.freedesktop.DBus.Error.NoReply"),
    ("private/path contains secret", "unclassified"),
    (None, "unclassified"),
])
def test_nm_failure_exposes_only_method_and_valid_remote_error_name(remote_name, expected):
    class Bus:
        def call_sync(self, *args):
            raise RuntimeError("SECRET /private/device/path must never appear")
    backend = object.__new__(GioNetworkBackend)
    backend.GLib = type("GLib", (), {"Variant": Variant})
    backend.Gio = type("Gio", (), {
        "DBusCallFlags": type("Flags", (), {"NONE": 0}),
        "DBusError": type("DBusError", (), {"get_remote_error": staticmethod(lambda error: remote_name)}),
    })
    backend.bus = Bus()
    with pytest.raises(HelperError) as failure:
        backend.call("/private/device/path", NM, "StartFind", AdvancingBudget(), "(a{sv})", ({},))
    assert failure.value.code == "NetworkFailure"
    assert failure.value.message == f"NetworkManager StartFind failed ({expected})"
    assert "SECRET" not in str(failure.value) and "/private" not in str(failure.value)


def test_nm_conflicting_home_selection_cancels_checkpoint_without_overwriting():
    backend = NMFixture()
    budget = Budget(1000, clock=Clock())
    snapshot = backend.snapshot(budget)
    backend.data["/active/home", NM + ".Connection.Active"]["Uuid"] = "33333333-3333-4333-8333-333333333333"
    errors = backend.restore({"snapshot": snapshot, "checkpoint": "/checkpoint/1"}, budget)
    assert "wifi-association-conflict" in errors
    assert ("CheckpointDestroy", ("/checkpoint/1",)) in backend.calls
    assert not any(method in ("Set", "ActivateConnection", "CheckpointRollback") for method, _ in backend.calls)


def test_nm_activation_timeout_marks_uncertain_ownership_for_bus_reset():
    backend = NMFixture()
    budget = Budget(1000, clock=Clock())
    snapshot = backend.snapshot(budget)
    backend.fail_method = "AddAndActivateConnection2"
    with pytest.raises(HelperError):
        backend.activate(snapshot, Backend().peers[0], "22222222-2222-4222-8222-222222222222", budget)
    assert backend.activation_uncertain


def test_caller_disappearing_before_discovery_cannot_acquire_lease(setup):
    helper, backend, _, _ = setup
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(HelperError, match="Cancelled"):
        helper.discover(":1.1", request_cancel=cancelled)
    assert backend.connected and not helper.status()["owner_present"]


def test_three_failed_connections_require_explicit_release_to_retry(setup):
    helper, backend, _, _ = setup
    backend.fail_connect = True
    for _ in range(3):
        candidate = helper.discover(":1.1")["candidates"][0]
        with pytest.raises(HelperError, match="ConnectionFailed"):
            helper.connect(":1.1", candidate["id"])
    assert helper.status()["retry_required"]
    with pytest.raises(HelperError, match="RetryRequired"):
        helper.discover(":1.1")
    helper.release(":1.1")
    backend.fail_connect = False
    candidate = helper.discover(":1.1")["candidates"][0]
    helper.connect(":1.1", candidate["id"])
    assert helper.status()["failed_attempts"] == 0


def test_journal_failure_does_not_become_success_on_next_release(setup):
    helper, backend, journal, _ = setup
    helper.discover(":1.1")
    original_write = journal.write
    def fail_write(record):
        raise OSError("disk unavailable")
    journal.write = fail_write
    assert helper.release(":1.1")["network_restored"] is False
    assert helper.release(":1.1")["network_restored"] is False
    journal.write = original_write
    assert helper.release(":1.1")["network_restored"] is True


def test_release_cancels_inflight_connect_while_status_stays_readable(setup):
    helper, backend, _, _ = setup
    candidate = helper.discover(":1.1")["candidates"][0]
    entered, resume = threading.Event(), threading.Event()
    backend.block = entered, resume
    errors, released = [], []
    def connect():
        try:
            helper.connect(":1.1", candidate["id"])
        except HelperError as error:
            errors.append(error.code)
    connecting = threading.Thread(target=connect)
    connecting.start()
    assert entered.wait(2)
    releasing = threading.Thread(target=lambda: released.append(helper.release(":1.1")))
    releasing.start()
    assert helper.cancel.wait(2)
    assert helper.status()["state"] == "connecting"
    resume.set()
    connecting.join(2)
    releasing.join(2)
    assert errors == ["Cancelled"]
    assert released[0]["network_restored"] is True and backend.active is None
