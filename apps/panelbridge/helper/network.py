"""Exclusive, checkpointed Wi-Fi Direct transactions.

Gio is imported only by the real backend. The transaction layer can be exercised
without Linux, root privileges or a network connection. No API accepts commands,
paths, interfaces or arbitrary NetworkManager settings from the client.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
import subprocess
import threading
import time
import uuid
from pathlib import Path

NM = "org.freedesktop.NetworkManager"
NM_PATH = "/org/freedesktop/NetworkManager"
FAMILY = re.compile(r"EBPS[I1]-[A-Za-z0-9]{1,64}\Z")
MAC = re.compile(r"[0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5}\Z")
INTERFACE = re.compile(r"[A-Za-z0-9_.-]{1,15}\Z")
UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
# Status exposes stage identifiers only; saved journal contents remain private.
RESTORATION_ERRORS = frozenset({
    "activation-owner-reset-failed", "device-inspection-failed",
    "recorded-network-device-missing", "discovery-stop-failed", "deactivation-failed",
    "p2p-drain-timed-out", "p2p-drain-conflict", "p2p-drain-failed",
    "wifi-inspection-failed", "checkpoint-cancel-failed", "wifi-association-conflict",
    "checkpoint-inspection-failed", "checkpoint-rollback-failed",
    "checkpoint-rollback-incomplete", "wifi-restore-failed",
    "network-verification-failed", "restoration-failed", "journal-write-failed",
})


class HelperError(Exception):
    """Only these bounded, deliberately non-sensitive errors cross D-Bus."""
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class Budget:
    def __init__(self, deadline, cancel=None, *, clock=time.monotonic):
        self.deadline = deadline
        self.cancel = cancel or threading.Event()
        self.clock = clock

    def check(self):
        if self.cancel.is_set():
            raise HelperError("Cancelled", "Operation cancelled; network restoration requested")
        if self.clock() >= self.deadline:
            raise HelperError("TimedOut", "Operation deadline reached")

    def timeout_ms(self, maximum=5000):
        self.check()
        return max(1, min(maximum, int((self.deadline - self.clock()) * 1000)))

    def pause(self, seconds=0.2):
        self.check()
        self.cancel.wait(min(seconds, max(0, self.deadline - self.clock())))
        self.check()


def sink_available(value):
    """Parse WFD subelements; require an available primary/dual-role sink."""
    try:
        data = bytes(value)
    except (ValueError, TypeError):
        return False
    if len(data) > 1024:
        return False
    cursor, available = 0, False
    while cursor < len(data):
        if len(data) - cursor < 3:
            return False
        kind = data[cursor]
        size = int.from_bytes(data[cursor + 1:cursor + 3], "big")
        cursor += 3
        if size > len(data) - cursor:
            return False
        if kind == 0:
            if size != 6:
                return False
            flags = int.from_bytes(data[cursor:cursor + 2], "big")
            port = int.from_bytes(data[cursor + 2:cursor + 4], "big")
            available = (flags & 3) in (1, 3) and bool(flags & 0x10) and port != 0
        cursor += size
    return available


def valid_peer(peer):
    return bool(peer and FAMILY.fullmatch(peer.get("name", ""))
                and MAC.fullmatch(peer.get("address", ""))
                and sink_available(peer.get("wfd", b"")))


def select_receiver_ip(gateway, addresses, stations, lease_text, now):
    """Derive one address from the negotiated link, never by guessing/scanning."""
    candidate = gateway
    if not candidate:
        if len(stations) != 1 or not MAC.fullmatch(stations[0]):
            raise HelperError("AddressUnavailable", "Expected exactly one associated receiver")
        matches = []
        for line in lease_text.splitlines():
            fields = line.split()
            if len(fields) < 3 or fields[1].lower() != stations[0].lower():
                continue
            try:
                if int(fields[0]) > now:
                    matches.append(fields[2])
            except ValueError:
                continue
        if len(matches) != 1:
            raise HelperError("AddressUnavailable", "Expected one current receiver DHCP lease")
        candidate = matches[0]
    try:
        ip = ipaddress.IPv4Address(candidate)
        locals_ = [ipaddress.IPv4Interface(f'{a["address"]}/{a["prefix"]}') for a in addresses]
    except (ValueError, TypeError, KeyError):
        raise HelperError("AddressUnavailable", "Invalid negotiated IPv4 address") from None
    if ip.is_multicast or ip.is_unspecified or ip.is_loopback or ip.is_link_local:
        raise HelperError("AddressUnavailable", "Receiver address is not a usable link address")
    if any(ip == local.ip for local in locals_):
        raise HelperError("AddressUnavailable", "Receiver address equals the local address")
    if not any(ip in local.network and ip not in (local.network.network_address,
                                                   local.network.broadcast_address)
               for local in locals_):
        raise HelperError("AddressUnavailable", "Receiver address is outside the selected link")
    return str(ip)


class NetworkHelper:
    """Serializes every mutation and restoration; cancellation is out-of-band."""
    def __init__(self, backend, journal, *, clock=time.monotonic):
        self.backend, self.journal, self.clock = backend, journal, clock
        self._operation = threading.Lock()
        self._state = threading.RLock()
        self.owner = None
        self.deadline = 0.0
        self.cancel = threading.Event()
        self.candidates = {}
        self.record = journal.read()
        self.last_error = None
        self.recovery = False
        self.failures = 0
        self.state = "restore_required" if self.record and self.record.get("state") != "restored" else "idle"

    def status(self, *, refresh=False):
        if refresh:
            try:
                self.recovery = self.backend.recovery_available(self._budget(5, cleanup=True))
            except Exception:
                self.recovery = False
        with self._state:
            recorded_errors = (self.record or {}).get("errors", []) if self.state == "restore_required" else []
            restoration_errors = list(dict.fromkeys(
                error for error in recorded_errors[:64]
                if isinstance(error, str) and error in RESTORATION_ERRORS
            )) if isinstance(recorded_errors, list) else []
            return {"api_version": 1, "state": self.state, "owner_present": self.owner is not None,
                    "recovery_available": self.recovery, "last_error": self.last_error,
                    "lease_deadline": self.deadline, "failed_attempts": self.failures,
                    "retry_required": self.failures >= 3, "restoration_errors": restoration_errors}

    def _budget(self, seconds, *, cleanup=False):
        return Budget(self.clock() + seconds, None if cleanup else self.cancel, clock=self.clock)

    def _enter(self, sender, *, acquire=False, request_cancel=None):
        if not self._operation.acquire(blocking=False):
            raise HelperError("Busy", "A network operation is already running")
        try:
            expired = False
            with self._state:
                if request_cancel is not None and request_cancel.is_set():
                    raise HelperError("Cancelled", "Caller disconnected before the operation began")
                if self.owner and self.owner != sender:
                    raise HelperError("Busy", "Another client owns the network lease")
                if self.state == "restore_required":
                    raise HelperError("RestoreRequired", "Restore the previous transaction before retrying")
                if self.owner and (self.cancel.is_set() or self.clock() >= self.deadline):
                    self.cancel.set()
                    expired = True
                if not self.owner:
                    if not acquire:
                        raise HelperError("NoLease", "Discover before connecting")
                    if self.failures >= 3:
                        raise HelperError("RetryRequired", "Three connections failed; explicitly release before retrying")
                    self.owner = sender
                    self.cancel = request_cancel or threading.Event()
                    self.deadline = self.clock() + 60
            if expired:
                self._restore()
                raise HelperError("NoLease", "The network lease expired")
        except BaseException:
            self._operation.release()
            raise

    def _save(self):
        self.journal.write(self.record)

    def _failure(self, error):
        self.last_error = error.code if isinstance(error, HelperError) else "NetworkFailure"
        self._restore()
        if isinstance(error, HelperError):
            raise error
        raise HelperError("NetworkFailure", "Network operation failed; inspect helper status") from None

    def discover(self, sender, *, request_cancel=None):
        self._enter(sender, acquire=True, request_cancel=request_cancel)
        try:
            if self.state == "connected":
                raise HelperError("Busy", "Release the current connection before discovery")
            # A rejected request has no new transaction to roll back. In
            # particular, the current owner's valid stream must keep its lease.
            try:
                budget = self._budget(20)
                if not self.record or self.record.get("state") == "restored":
                    snapshot = self.backend.snapshot(budget)
                    self.recovery = True
                    self.record = {"api_version": 1, "state": "preparing", "snapshot": snapshot,
                                   "checkpoint": None, "connection_uuid": None}
                    self._save()  # durable baseline exists before the first mutation
                    self.record["checkpoint"] = self.backend.checkpoint_create(snapshot, budget)
                    self._save()
                    self.backend.prepare(snapshot, budget)
                self.state = "discovering"
                peers = self.backend.discover(self.record["snapshot"], budget)
                budget.check()
                candidates = {}
                for peer in peers[:64]:
                    if valid_peer(peer):
                        candidate_id = uuid.uuid4().hex
                        candidates[candidate_id] = dict(peer)
                self.candidates = candidates
                self.record["state"] = self.state = "discovered"
                self._save()
                return {"api_version": 1, "lease_deadline": self.deadline, "candidates": [
                    {"id": key, "name": p["name"], "manufacturer": p.get("manufacturer", "")[:128],
                     "peer_address": p["address"].lower(), "transport": "miracast", "wfd_available": True}
                    for key, p in candidates.items()]}
            except Exception as error:
                self._failure(error)
        finally:
            self._operation.release()

    def connect(self, sender, candidate_id):
        self._enter(sender)
        try:
            if self.state != "discovered" or candidate_id not in self.candidates:
                raise HelperError("StaleCandidate", "Select a current discovery candidate")
            # Old IDs and duplicate requests cannot release a current lease or
            # consume a connection attempt. _enter still restores expired leases.
            try:
                budget = self._budget(45)
                candidate = self.candidates[candidate_id]
                peer = self.backend.resolve_peer(self.record["snapshot"], candidate["address"], budget)
                if not valid_peer(peer) or peer["name"] != candidate["name"]:
                    raise HelperError("StaleCandidate", "Selected receiver is absent or no longer an available sink")
                if not self.backend.recovery_available(budget):
                    raise HelperError("RecoveryRequired", "Connected Ethernet recovery is required")
                self.backend.checkpoint_extend(self.record["checkpoint"], budget)
                self.record["connection_uuid"] = str(uuid.uuid4())
                self.record["state"] = self.state = "connecting"
                self._save()  # UUID journals ownership even if the activation reply times out
                result = self.backend.activate(self.record["snapshot"], peer,
                                               self.record["connection_uuid"], budget)
                budget.check()
                if self.clock() >= self.deadline:
                    raise HelperError("NoLease", "The network lease expired during connection")
                self.record["group_interface"] = result["group_interface"]
                self.record["state"] = self.state = "connected"
                self.failures = 0
                self.last_error = None
                self._save()
                return {"api_version": 1, "peer_name": peer["name"], "peer_address": peer["address"],
                        "p2p_interface": self.record["snapshot"]["p2p_interface"],
                        "connection_uuid": self.record["connection_uuid"], "lease_deadline": self.deadline,
                        **result}
            except Exception as error:
                self.failures += 1
                self._failure(error)
        finally:
            self._operation.release()

    def keep_alive(self, sender):
        self._enter(sender)
        try:
            self.backend.checkpoint_extend(self.record["checkpoint"], self._budget(5))
            self._budget(1).check()
            with self._state:
                self.deadline = self.clock() + 60
            return {"api_version": 1, "lease_deadline": self.deadline}
        except Exception as error:
            self._failure(error)
        finally:
            self._operation.release()

    def cancel_owner(self, sender):
        with self._state:
            if sender is not None and self.owner == sender:
                self.cancel.set()
                return True
            return False

    def _restore(self):
        errors = []
        self.state = "restoring"
        if self.record and self.record.get("state") != "restored":
            try:
                errors = self.backend.restore(self.record, self._budget(35, cleanup=True))
            except Exception:
                errors = ["restoration-failed"]
            self.record["state"] = "restore_failed" if errors else "restored"
            self.record["errors"] = errors
            try:
                self._save()
            except Exception:
                errors = [*errors, "journal-write-failed"]
                self.record["state"] = "restore_failed"
                self.record["errors"] = errors
        with self._state:
            self.owner, self.deadline, self.candidates = None, 0.0, {}
            self.state = "restore_required" if errors else "idle"
            if errors:
                self.last_error = "RestoreRequired"
            elif self.last_error == "RestoreRequired":
                self.last_error = None
        return {"api_version": 1, "network_restored": not errors, "errors": errors}

    def release(self, sender):
        with self._state:
            if self.owner and self.owner != sender:
                raise HelperError("Busy", "Another client owns the network lease")
            self.cancel.set()
        with self._operation:
            with self._state:
                if self.owner and self.owner != sender:
                    raise HelperError("Busy", "Another client acquired the network lease")
            result = self._restore()
            if result["network_restored"]:
                self.failures = 0
            return result

    def expire(self):
        with self._state:
            if not self.owner or self.clock() < self.deadline:
                return False
            sender = self.owner
            self.cancel.set()
        self.release(sender)
        return True

    def recover(self):
        with self._operation:
            result = self._restore()
            try:
                self.recovery = self.backend.recovery_available(self._budget(5, cleanup=True))
            except Exception:
                self.recovery = False
            return result


class GioNetworkBackend:
    """The production NM adapter; each bus call and process has a finite budget."""
    def __init__(self, bus=None):
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
        self.Gio, self.GLib = Gio, GLib
        self.activation_uncertain = False
        self.bus = bus or self.new_bus(Budget(time.monotonic() + 5))

    def _bounded_bus_operation(self, operation, budget):
        cancellable = self.Gio.Cancellable.new()
        timer = threading.Timer(budget.timeout_ms() / 1000, cancellable.cancel)
        timer.daemon = True
        timer.start()
        try:
            return operation(cancellable)
        except Exception:
            raise HelperError("NetworkFailure", "System bus connection failed") from None
        finally:
            timer.cancel()

    def new_bus(self, budget):
        # A private connection makes bind-activation belong to this adapter,
        # independently of the service's public D-Bus connection.
        return self._bounded_bus_operation(lambda cancel:
            self.Gio.DBusConnection.new_for_address_sync(
                "unix:path=/run/dbus/system_bus_socket",
                self.Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | self.Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None, cancel), budget)

    def reset_bus(self, budget):
        if not self.bus.is_closed():
            self._bounded_bus_operation(lambda cancel: self.bus.close_sync(cancel), budget)
        self.bus = self.new_bus(budget)
        self.activation_uncertain = False

    def call(self, path, interface, method, budget, signature=None, args=None):
        value = self.GLib.Variant(signature, args) if signature else None
        try:
            # Do not cancel an in-flight mutation: receive its result before teardown.
            return self.bus.call_sync(NM, path, interface, method, value, None,
                                      self.Gio.DBusCallFlags.NONE, budget.timeout_ms(), None).unpack()
        except HelperError:
            raise
        except Exception as error:
            # GError text can contain interface paths and private connection
            # values. Only expose the protocol error name, never its message.
            remote_name = None
            try:
                remote_name = self.Gio.DBusError.get_remote_error(error)
            except Exception:
                pass
            if (not isinstance(remote_name, str) or len(remote_name) > 180
                    or not re.fullmatch(r"org\.freedesktop\.(?:NetworkManager|DBus)(?:\.[A-Za-z_][A-Za-z0-9_]*)+", remote_name)):
                remote_name = "unclassified"
            safe_method = method if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", method) else "request"
            raise HelperError("NetworkFailure", f"NetworkManager {safe_method} failed ({remote_name})") from None

    def props(self, path, interface, budget):
        return self.call(path, "org.freedesktop.DBus.Properties", "GetAll", budget,
                         "(s)", (interface,))[0]

    def devices(self, budget):
        paths = self.call(NM_PATH, NM, "GetDevices", budget)[0]
        if len(paths) > 64:
            raise HelperError("UnsupportedNetwork", "Too many network devices")
        return [(path, self.props(path, NM + ".Device", budget)) for path in paths]

    def recovery_available(self, budget):
        ethernet = [p["IpInterface"] for _, p in self.devices(budget)
                    if p.get("DeviceType") == 1 and p.get("State") == 100
                    and p.get("Ip4Config", "/") != "/"]
        if not ethernet:
            return False
        # Activation/IP configuration can precede route installation at boot.
        # Use snapshot's existing preferred-route rule under the same budget.
        routes = self.routes(budget)
        return bool(routes) and min(routes, key=lambda r: r.get("metric", 0)).get("dev") in ethernet

    @staticmethod
    def command(args, budget):
        try:
            result = subprocess.run(args, capture_output=True, timeout=budget.timeout_ms() / 1000,
                                    env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                                    stdin=subprocess.DEVNULL, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise HelperError("InspectionFailed", "Bounded network inspection failed") from None
        if result.returncode or len(result.stdout) > 65536:
            raise HelperError("InspectionFailed", "Bounded network inspection failed")
        return result.stdout.decode("utf-8", errors="replace")

    def routes(self, budget):
        try:
            routes = json.loads(self.command(["/usr/sbin/ip", "-j", "route", "show", "default"], budget))
        except ValueError:
            raise HelperError("InspectionFailed", "Could not inspect default routes") from None
        if not isinstance(routes, list) or len(routes) > 64:
            raise HelperError("InspectionFailed", "Unexpected default route data")
        return routes

    def snapshot(self, budget):
        devices = self.devices(budget)
        wifi = [(p, d) for p, d in devices if d.get("DeviceType") == 2]
        p2p = [(p, d) for p, d in devices if d.get("DeviceType") == 30]
        ethernet = [d["IpInterface"] for _, d in devices if d.get("DeviceType") == 1
                    and d.get("State") == 100 and d.get("Ip4Config", "/") != "/"]
        if len(wifi) != 1 or len(p2p) != 1:
            raise HelperError("UnsupportedNetwork", "Expected one Wi-Fi and one P2P device")
        wp, wd = wifi[0]
        pp, pd = p2p[0]
        if not INTERFACE.fullmatch(wd.get("Interface", "")) or pd.get("Interface") != "p2p-dev-" + wd["Interface"]:
            raise HelperError("UnsupportedNetwork", "P2P device is not bound to the selected Wi-Fi interface")
        if not wd.get("Managed") or not pd.get("Managed") or pd.get("ActiveConnection", "/") != "/":
            raise HelperError("Busy", "Wi-Fi devices are unmanaged or P2P is already in use")
        if wd.get("State") not in (30, 100):
            raise HelperError("Busy", "Wi-Fi is in a transitional state")
        routes = self.routes(budget)
        if not ethernet or not routes or min(routes, key=lambda r: r.get("metric", 0)).get("dev") not in ethernet:
            raise HelperError("RecoveryRequired", "Connected Ethernet with the current default route is required")
        active = wd.get("ActiveConnection", "/")
        old_uuid = self.props(active, NM + ".Connection.Active", budget).get("Uuid") if active != "/" else None
        return {"wifi": wp, "p2p": pp, "wifi_interface": wd["Interface"],
                "p2p_interface": pd["Interface"], "wifi_uuid": old_uuid,
                "wifi_state": wd["State"], "wifi_autoconnect": wd["Autoconnect"],
                "default_routes": [r for r in routes if r.get("dev") in ethernet]}

    def checkpoint_create(self, snapshot, budget):
        return self.call(NM_PATH, NM, "CheckpointCreate", budget, "(aouu)",
                         ([snapshot["wifi"]], 120, 0))[0]

    def checkpoint_extend(self, checkpoint, budget):
        try:
            self.call(NM_PATH, NM, "CheckpointAdjustRollbackTimeout", budget, "(ou)", (checkpoint, 120))
        except HelperError:
            raise HelperError("CheckpointFailed", "Checkpoint could not be renewed") from None

    def prepare(self, snapshot, budget):
        current = self.props(snapshot["wifi"], NM + ".Device", budget)
        if current.get("State") == 30 and current.get("ActiveConnection") == "/":
            # Ethernet-only hosts already have an idle station interface. NM
            # rejects Disconnect here; keep it idle for P2P discovery instead.
            if current.get("Autoconnect") is not False:
                self.call(snapshot["wifi"], "org.freedesktop.DBus.Properties", "Set", budget,
                          "(ssv)", (NM + ".Device", "Autoconnect", self.GLib.Variant("b", False)))
            return
        self.call(snapshot["wifi"], NM + ".Device", "Disconnect", budget)

    def _peers(self, snapshot, budget):
        paths = self.props(snapshot["p2p"], NM + ".Device.WifiP2P", budget).get("Peers", [])
        if len(paths) > 64:
            raise HelperError("DiscoveryFailed", "Peer discovery limit exceeded")
        peers = []
        for path in paths:
            values = self.props(path, NM + ".WifiP2PPeer", budget)
            peers.append({"path": path, "name": values.get("Name", ""),
                          "address": values.get("HwAddress", "").lower(),
                          "manufacturer": values.get("Manufacturer", "")[:128],
                          "wfd": values.get("WfdIEs", b"")})
        return peers

    def discover(self, snapshot, budget):
        self.call(snapshot["p2p"], NM + ".Device.WifiP2P", "StartFind", budget,
                  "(a{sv})", ({"timeout": self.GLib.Variant("i", max(1, int(budget.deadline - budget.clock())))},))
        # Collect a short additional window after the first peer, allowing choice.
        settle = budget.deadline - 1
        peers = []
        while budget.clock() < min(settle, budget.deadline - 1):
            peers = [p for p in self._peers(snapshot, budget) if valid_peer(p)]
            if peers:
                settle = min(settle, budget.clock() + 1)
            budget.pause()
        self.call(snapshot["p2p"], NM + ".Device.WifiP2P", "StopFind", budget)
        return peers

    def resolve_peer(self, snapshot, address, budget):
        matches = [p for p in self._peers(snapshot, budget) if p["address"] == address.lower()]
        return matches[0] if len(matches) == 1 else None

    def activate(self, snapshot, peer, connection_uuid, budget):
        V = self.GLib.Variant
        settings = {
            "connection": {"id": V("s", "panelbridge-wireless"), "uuid": V("s", connection_uuid),
                           "type": V("s", "wifi-p2p"), "autoconnect": V("b", False)},
            "wifi-p2p": {"peer": V("s", peer["address"]),
                         "wfd-ies": V("ay", bytes.fromhex("00000600901c4400c8"))},
            "ipv4": {"method": V("s", "auto"), "never-default": V("b", True)},
            "ipv6": {"method": V("s", "auto"), "never-default": V("b", True), "may-fail": V("b", True)},
        }
        try:
            _, active, _ = self.call(NM_PATH, NM, "AddAndActivateConnection2", budget,
                                     "(a{sa{sv}}ooa{sv})", (settings, snapshot["p2p"], peer["path"],
                                     {"persist": V("s", "volatile"), "bind-activation": V("s", "dbus-client")}))
        except Exception:
            # A timed-out call may still complete in NM. Closing its unique bus
            # owner before teardown prevents a late activation after restoration.
            self.activation_uncertain = True
            raise
        while True:
            state = self.props(active, NM + ".Connection.Active", budget).get("State")
            if state == 2:
                break
            if state in (3, 4):
                raise HelperError("ConnectionFailed", "Receiver connection failed")
            budget.pause()
        device = self.props(snapshot["p2p"], NM + ".Device", budget)
        interface = device.get("IpInterface", "")
        expected = r"p2p-" + re.escape(snapshot["wifi_interface"]) + r"-[0-9]+"
        if not INTERFACE.fullmatch(interface) or not re.fullmatch(expected, interface):
            raise HelperError("AddressUnavailable", "Unexpected selected P2P group interface")
        ip4 = self.props(device["Ip4Config"], NM + ".IP4Config", budget)
        gateway = ip4.get("Gateway", "")
        while True:
            stations, leases = [], ""
            if not gateway:
                station_dump = self.command(["/usr/sbin/iw", "dev", interface, "station", "dump"], budget)
                stations = re.findall(r"^Station ([0-9a-fA-F:]{17}) ", station_dump, re.MULTILINE)
                if len(stations) > 1:
                    raise HelperError("AddressUnavailable", "More than one station joined the selected group")
                leases = self.lease_text(interface)
            try:
                peer_ip = select_receiver_ip(gateway, ip4.get("AddressData", []), stations, leases, time.time())
                break
            except HelperError:
                if gateway:
                    raise
                budget.pause()
        try:
            route = json.loads(self.command(["/usr/sbin/ip", "-j", "route", "get", peer_ip], budget))
        except ValueError:
            raise HelperError("AddressUnavailable", "Receiver route could not be inspected") from None
        if len(route) != 1 or route[0].get("dev") != interface:
            raise HelperError("AddressUnavailable", "Receiver route does not use the selected group")
        if not all(r in self.routes(budget) for r in snapshot["default_routes"]):
            raise HelperError("RouteChanged", "Ethernet default route changed during setup")
        return {"peer_ip": peer_ip, "group_interface": interface}

    @staticmethod
    def lease_text(interface):
        if not INTERFACE.fullmatch(interface):
            raise HelperError("AddressUnavailable", "Invalid selected group interface")
        path = Path("/var/lib/NetworkManager") / ("dnsmasq-" + interface + ".leases")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                    raise HelperError("AddressUnavailable", "Untrusted network lease file")
                value = stream.read(65537)
            if len(value) > 65536:
                raise HelperError("AddressUnavailable", "Network lease file is too large")
            return value.decode("ascii", errors="replace")
        except FileNotFoundError:
            return ""
        except OSError:
            raise HelperError("AddressUnavailable", "Network lease file could not be inspected") from None

    def restore(self, record, budget):
        """Attempt independent cleanup stages even if an earlier stage failed."""
        errors = []
        snapshot = record["snapshot"]
        def stage(label, action):
            try:
                return action()
            except Exception:
                errors.append(label)
                return None
        if self.activation_uncertain:
            stage("activation-owner-reset-failed", lambda: self.reset_bus(budget))
            if self.activation_uncertain:
                return errors
        # Resolve the actual device again: NM paths can change across daemon restart.
        devices = stage("device-inspection-failed", lambda: self.devices(budget))
        if devices is None:
            return errors
        wifi = [p for p, d in devices if d.get("DeviceType") == 2 and d.get("Interface") == snapshot["wifi_interface"]]
        p2p = [p for p, d in devices if d.get("DeviceType") == 30 and d.get("Interface") == snapshot["p2p_interface"]]
        if len(wifi) != 1 or len(p2p) != 1:
            return [*errors, "recorded-network-device-missing"]
        wifi, p2p = wifi[0], p2p[0]
        stage("discovery-stop-failed", lambda: self.call(p2p, NM + ".Device.WifiP2P", "StopFind", budget))
        owned_uuid = record.get("connection_uuid")
        owned_group = record.get("group_interface")
        def deactivate_owned():
            nonlocal owned_group
            if not owned_uuid:
                return
            paths = self.props(NM_PATH, NM, budget).get("ActiveConnections", [])
            for path in paths[:64]:
                props = self.props(path, NM + ".Connection.Active", budget)
                if props.get("Uuid") == owned_uuid and props.get("Type") == "wifi-p2p":
                    device = self.props(p2p, NM + ".Device", budget)
                    if device.get("ActiveConnection", "/") != path:
                        continue
                    if owned_group is None:
                        owned_group = device.get("IpInterface") or None
                    self.call(NM_PATH, NM, "DeactivateConnection", budget, "(o)", (path,))
        stage("deactivation-failed", deactivate_owned)
        # NM accepts deactivation before the supplicant group/netdev is gone.
        # Reserve most of the existing cleanup budget for home Wi-Fi recovery.
        drain_seconds = min(5, max(0, budget.deadline - budget.clock()) / 4)
        drain_budget = Budget(budget.clock() + drain_seconds, clock=budget.clock)
        def drain_owned():
            if owned_group is not None and (
                    not isinstance(owned_group, str) or not INTERFACE.fullmatch(owned_group)
                    or not re.fullmatch(r"p2p-" + re.escape(snapshot["wifi_interface"]) + r"-[0-9]+", owned_group)):
                raise HelperError("RestoreConflict", "Recorded group interface is invalid")
            if owned_group is not None:
                # _restore saves this even after failure, so a retry still checks
                # the exact netdev after NM forgets the old active connection.
                record["group_interface"] = owned_group
            while True:
                device = self.props(p2p, NM + ".Device", drain_budget)
                active = device.get("ActiveConnection", "/")
                if active != "/":
                    try:
                        connection = self.props(active, NM + ".Connection.Active", drain_budget)
                    except HelperError:
                        if self.props(p2p, NM + ".Device", drain_budget).get("ActiveConnection", "/") == active:
                            raise
                        continue
                    if not owned_uuid or connection.get("Uuid") != owned_uuid or connection.get("Type") != "wifi-p2p":
                        raise HelperError("P2PDrainConflict", "Another connection owns the P2P device")
                settled = device.get("State") == 30 and active == "/" and device.get("IpInterface") == ""
                if settled:
                    if owned_group is None:
                        return
                    links = json.loads(self.command(["/usr/sbin/ip", "-j", "link", "show"], drain_budget))
                    if (not isinstance(links, list) or len(links) > 64
                            or any(not isinstance(link, dict) or not isinstance(link.get("ifname"), str)
                                   or not INTERFACE.fullmatch(link["ifname"]) for link in links)):
                        raise HelperError("InspectionFailed", "Invalid bounded interface inspection")
                    if not any(link["ifname"] == owned_group for link in links):
                        return
                budget.pause(min(0.2, max(0, drain_budget.deadline - budget.clock())))
                drain_budget.check()
        try:
            drain_owned()
        except HelperError as error:
            if error.code == "TimedOut" or budget.clock() >= drain_budget.deadline:
                errors.append("p2p-drain-timed-out")
            else:
                errors.append("p2p-drain-conflict" if error.code == "P2PDrainConflict" else "p2p-drain-failed")
        except Exception:
            errors.append("p2p-drain-failed")
        def wifi_view():
            while True:
                current = self.props(wifi, NM + ".Device", budget)
                active = current.get("ActiveConnection", "/")
                try:
                    current_uuid = self.props(active, NM + ".Connection.Active", budget).get("Uuid") if active != "/" else None
                except HelperError:
                    # The old active object can vanish while Disconnect finishes.
                    # Retry only when a fresh device read proves that it changed.
                    updated = self.props(wifi, NM + ".Device", budget)
                    if (updated.get("ActiveConnection", "/") != active
                            or updated.get("State") != current.get("State")):
                        budget.pause()
                        continue
                    raise
                return current, current_uuid
        observed = stage("wifi-inspection-failed", wifi_view)
        if observed is None:
            return errors
        current, current_uuid = observed
        checkpoint = record.get("checkpoint")
        def cancel_surviving_checkpoint():
            def cancel():
                if checkpoint and checkpoint in self.props(NM_PATH, NM, budget).get("Checkpoints", []):
                    self.call(NM_PATH, NM, "CheckpointDestroy", budget, "(o)", (checkpoint,))
            stage("checkpoint-cancel-failed", cancel)
        if current_uuid and current_uuid != snapshot.get("wifi_uuid"):
            # Cancel automatic rollback before returning a conflict, so a later NM
            # timeout cannot overwrite the user's newly selected association.
            cancel_surviving_checkpoint()
            return [*errors, "wifi-association-conflict"]
        if checkpoint:
            checkpoints = stage("checkpoint-inspection-failed", lambda: self.props(NM_PATH, NM, budget).get("Checkpoints", []))
            if checkpoints is not None and checkpoint in checkpoints:
                result = stage("checkpoint-rollback-failed", lambda: self.call(NM_PATH, NM, "CheckpointRollback", budget, "(o)", (checkpoint,))[0])
                if result is not None and (not result or any(value != 0 for value in result.values())):
                    errors.append("checkpoint-rollback-incomplete")
        def restore_wifi():
            original_uuid = snapshot.get("wifi_uuid")
            if snapshot["wifi_state"] == 100 and (not original_uuid or not UUID.fullmatch(original_uuid)):
                raise HelperError("RestoreConflict", "Original connection identity is missing")
            activation_requested = False
            autoconnect_set = False
            while True:
                current, actual_uuid = wifi_view()
                active = current.get("ActiveConnection", "/")
                if actual_uuid and actual_uuid != original_uuid:
                    cancel_surviving_checkpoint()
                    raise HelperError("RestoreConflict", "Wi-Fi association changed")
                state = current.get("State")
                if state == snapshot["wifi_state"] and actual_uuid == original_uuid:
                    if current.get("Autoconnect") == snapshot["wifi_autoconnect"]:
                        return
                    if not autoconnect_set:
                        self.call(wifi, "org.freedesktop.DBus.Properties", "Set", budget, "(ssv)",
                                  (NM + ".Device", "Autoconnect", self.GLib.Variant("b", snapshot["wifi_autoconnect"])))
                        autoconnect_set = True
                elif (state == 30 and active == "/" and snapshot["wifi_state"] == 100
                        and not activation_requested):
                    connection = self.call(NM_PATH + "/Settings", NM + ".Settings", "GetConnectionByUuid", budget,
                                           "(s)", (original_uuid,))[0]
                    self.call(NM_PATH, NM, "ActivateConnection", budget, "(ooo)", (connection, wifi, "/"))
                    activation_requested = True
                # A successful rollback reply is not a settled Device state.
                # In particular state 110 may still expose the old active UUID.
                # Wait for it to disconnect, then evaluate restoration again.
                budget.pause()
        stage("wifi-restore-failed", restore_wifi)
        def verify_p2p():
            waiting_for_route = False
            while True:
                if self.props(p2p, NM + ".Device", budget).get("ActiveConnection", "/") != "/":
                    raise HelperError("RestoreConflict", "P2P remains active")
                if waiting_for_route:
                    current, actual_uuid = wifi_view()
                    if (current.get("State") != snapshot["wifi_state"]
                            or actual_uuid != snapshot.get("wifi_uuid")
                            or current.get("Autoconnect") != snapshot["wifi_autoconnect"]):
                        raise HelperError("RestoreConflict", "Wi-Fi changed during route verification")
                routes = self.routes(budget)
                if all(route in routes for route in snapshot.get("default_routes", [])):
                    return
                if errors:
                    raise HelperError("RestoreConflict", "Ethernet route was not preserved")
                # NM may expose settled Wi-Fi before Ethernet receives its boot
                # route. Only read here, using the original cleanup deadline;
                # never replay mutations or hide an earlier restoration error.
                waiting_for_route = True
                budget.pause(min(0.2, max(0, budget.deadline - budget.clock())))
        stage("network-verification-failed", verify_p2p)
        return errors
