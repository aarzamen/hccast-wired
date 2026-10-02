"""Synthetic networking and real owned pipes/processes; no device access."""

import asyncio
import importlib.util
import os
from pathlib import Path
import signal
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.rescue import CairoCard, RescueError, RescueRuntime, RescueTimeouts, write_frame


ADDRESS = "02:00:00:00:00:01"
FRAME = bytes((32, 32, 32, 255)) * (1280 * 720)


class Network:
    def __init__(self, *, failures=0, addresses=(ADDRESS,), restored=True):
        self.failures, self.addresses, self.restored = failures, addresses, restored
        self.connects = self.releases = self.closes = self.heartbeats = 0
        self.calls = []
        self.entered = asyncio.Event()
        self.hold = None

    async def discover(self):
        self.calls.append("discover")
        return {"api_version": 1, "lease_deadline": time.monotonic() + 60,
                "candidates": [{"id": str(i), "peer_address": address, "name": "EBPSI-TEST",
                                "transport": "miracast", "wfd_available": True}
                               for i, address in enumerate(self.addresses)]}

    async def connect(self, candidate):
        self.calls.append("connect")
        self.connects += 1
        self.entered.set()
        if self.hold:
            await self.hold.wait()
        if self.connects <= self.failures:
            raise RuntimeError("private diagnostic that must not appear on the card")
        return {"api_version": 1, "peer_address": self.addresses[int(candidate)],
                "peer_name": "EBPSI-TEST", "peer_ip": "192.0.2.2", "p2p_interface": "p2p-test0",
                "connection_uuid": "00000000-0000-4000-8000-000000000001",
                "lease_deadline": time.monotonic() + 60}

    async def keep_alive(self):
        self.heartbeats += 1
        return {"api_version": 1, "lease_deadline": time.monotonic() + 60}

    async def release(self):
        self.calls.append("release")
        self.releases += 1
        return {"api_version": 1, "network_restored": self.restored,
                "errors": [] if self.restored else ["conflict"]}

    async def close(self):
        self.closes += 1
        self.calls.append("close")


async def healthy():
    return {"temperature_c": 55.0, "throttled_bits": 0, "cpu_percent": None}


def worker(tmp_path, mode="normal"):
    executable = tmp_path / "worker"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, signal, sys, time
args = sys.argv[1:]
fd = int(args[args.index('--fd') + 1])
assert args[args.index('--source') + 1] == 'raw'
assert args[args.index('--width') + 1] == '1280'
assert args[args.index('--height') + 1] == '720'
assert args[args.index('--fps') + 1] == '5'
assert args[args.index('--wire-fps') + 1] == '30'
assert args[args.index('--peer-address') + 1] == '02:00:00:00:00:01'
assert '--continuous' in args and '--seconds' not in args
assert not any(key in os.environ for key in ('WAYLAND_DISPLAY', 'DISPLAY', 'DBUS_SESSION_BUS_ADDRESS'))
mode = ''' + repr(mode) + '''
if mode == 'ignore-term':
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if mode != 'not-ready':
    print(json.dumps({'event': 'state', 'detail': 'ND_SINK_STATE_STREAMING'}), flush=True)
if mode in ('blocked', 'not-ready'):
    time.sleep(60)
count = 0
while True:
    remaining = 1280 * 720 * 4
    while remaining:
        data = os.read(fd, min(65536, remaining))
        if not data:
            if mode == 'ignore-term':
                time.sleep(60)
            sys.exit(0)
        remaining -= len(data)
    count += 1
    print(json.dumps({'event': 'pipeline-telemetry', 'encoded_buffers': count}), flush=True)
    if mode == 'exit':
        sys.exit(2)
''')
    executable.chmod(0o700)
    return executable


def runtime(tmp_path, network=None, **kwargs):
    return RescueRuntime(
        network or Network(), receiver_address=ADDRESS, expected_uid=os.getuid(),
        health=kwargs.pop("health", healthy), worker=worker(tmp_path, kwargs.pop("mode", "normal")),
        renderer=lambda seconds, status: FRAME,
        timeouts=RescueTimeouts(discover=.1, connect=.1, startup=.5, heartbeat=.05,
                               heartbeat_interval=.05, health=.05, health_interval=.02,
                               stall=.5, write=.5, cleanup=.5, retry_delay=.01,
                               terminate=.03, kill=.1), **kwargs,
    )


def test_partial_nonblocking_writes_preserve_complete_frame():
    async def scenario():
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        received = bytearray()

        async def consume():
            while len(received) < len(FRAME):
                try:
                    received.extend(os.read(read_fd, 733))
                except BlockingIOError:
                    await asyncio.sleep(0)

        try:
            await asyncio.wait_for(asyncio.gather(write_frame(write_fd, FRAME, timeout=2), consume()), 3)
            assert received == FRAME
        finally:
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(scenario())


def test_blocked_pipe_times_out_and_cancel_removes_writer_registration():
    async def scenario():
        read_fd, write_fd = os.pipe()
        try:
            with pytest.raises(TimeoutError):
                await write_frame(write_fd, FRAME, timeout=.02)
            task = asyncio.create_task(write_frame(write_fd, FRAME, timeout=2))
            await asyncio.sleep(.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not asyncio.get_running_loop().remove_writer(write_fd)
        finally:
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(scenario())


def test_finite_runtime_reaps_worker_and_restores_network(tmp_path):
    async def scenario():
        rescue = runtime(tmp_path)
        result = await rescue.run(seconds=.7)
        assert result["reason"] == "duration_limit"
        assert result["frames_written"] >= 1
        assert result["network_restored"] is True
        assert rescue.worker_process.returncode is not None
        assert not rescue.processes.children
        assert rescue.network.closes == 1
        assert rescue.network.heartbeats >= 2
    asyncio.run(scenario())


def test_three_failed_connections_stop_without_rearming_helper_between_attempts(tmp_path):
    async def scenario():
        network = Network(failures=99)
        rescue = runtime(tmp_path, network)
        with pytest.raises(RescueError, match="attempts_exhausted"):
            await rescue.run(seconds=2)
        assert network.connects == 3
        assert network.calls == ["discover", "connect"] * 3 + ["release", "close"]
        assert not rescue.processes.children
    asyncio.run(scenario())


def test_never_substitutes_another_receiver_or_accepts_duplicate_binding(tmp_path):
    async def scenario():
        for addresses in (("02:00:00:00:00:02",), (ADDRESS, ADDRESS)):
            network = Network(addresses=addresses)
            rescue = runtime(tmp_path, network)
            with pytest.raises(RescueError):
                await rescue.run(seconds=1)
            assert network.connects == 0
            assert network.closes == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("health", [
    {"temperature_c": None, "throttled_bits": 0},
    {"temperature_c": 70.0, "throttled_bits": 0},
    {"temperature_c": 50.0, "throttled_bits": None},
    {"temperature_c": float("nan"), "throttled_bits": 0},
    {"temperature_c": 50.0, "throttled_bits": 2},
    {"temperature_c": 50.0, "throttled_bits": 0x20},
])
def test_unknown_or_unsafe_health_never_starts_network(tmp_path, health):
    async def sample():
        return health

    async def scenario():
        rescue = runtime(tmp_path, health=sample)
        with pytest.raises(RescueError, match="health"):
            await rescue.run(seconds=1)
        assert rescue.network.connects == 0
        assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_new_sticky_fault_stops_even_when_no_active_fault_remains(tmp_path):
    async def scenario():
        samples = 0

        async def sample():
            nonlocal samples
            samples += 1
            return {"temperature_c": 50, "throttled_bits": 0x10000 if samples < 4 else 0x50000}

        rescue = runtime(tmp_path, health=sample)
        with pytest.raises(RescueError, match="health"):
            await rescue.run(seconds=1)
        assert rescue.worker_process.returncode is not None
        assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_cancellation_during_connect_releases_and_closes_owner(tmp_path):
    async def scenario():
        network = Network()
        network.hold = asyncio.Event()
        rescue = runtime(tmp_path, network)
        task = asyncio.create_task(rescue.run(seconds=2))
        await network.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert network.calls[-2:] == ["release", "close"]
        assert not rescue.processes.children
    asyncio.run(scenario())


def test_restore_failure_is_reported_and_owner_still_closed(tmp_path):
    async def scenario():
        rescue = runtime(tmp_path, Network(restored=False))
        with pytest.raises(RescueError, match="network_restore_failed"):
            await rescue.run(seconds=.1)
        assert rescue.network.closes == 1
        assert not rescue.processes.children
    asyncio.run(scenario())


def test_stalled_worker_and_broken_worker_are_bounded_and_reaped(tmp_path):
    async def scenario():
        for mode in ("blocked", "not-ready", "exit", "ignore-term"):
            rescue = runtime(tmp_path, mode=mode)
            if mode == "ignore-term":
                await rescue.run(seconds=.1)
                assert rescue.worker_process.returncode == -signal.SIGKILL
            else:
                with pytest.raises(RescueError, match="attempts_exhausted"):
                    await rescue.run(seconds=4)
            assert rescue.worker_process.returncode is not None
            assert not rescue.processes.children
            assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_wrong_uid_refuses_before_network_access(tmp_path, monkeypatch):
    async def scenario():
        rescue = runtime(tmp_path)
        monkeypatch.setattr("panelbridge.rescue.os.geteuid", lambda: 0)
        with pytest.raises(RescueError, match="identity"):
            await rescue.run(seconds=1)
        assert rescue.network.calls == []
    asyncio.run(scenario())


def test_hung_health_sample_stops_before_network_access(tmp_path):
    async def sample():
        await asyncio.Event().wait()

    async def scenario():
        rescue = runtime(tmp_path, health=sample)
        with pytest.raises(RescueError, match="health"):
            await asyncio.wait_for(rescue.run(seconds=1), .5)
        assert rescue.network.connects == 0
        assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_hung_lease_renewal_reaps_each_attempt(tmp_path):
    class HungLease(Network):
        async def keep_alive(self):
            if self.heartbeats >= 2:
                await asyncio.Event().wait()
            return await super().keep_alive()

    async def scenario():
        rescue = runtime(tmp_path, HungLease())
        with pytest.raises(RescueError, match="attempts_exhausted"):
            await asyncio.wait_for(rescue.run(seconds=2), 3)
        assert rescue.worker_process.returncode is not None
        assert not rescue.processes.children
        assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_connection_budget_is_followed_by_lease_renewal_before_worker_start(tmp_path):
    class ExpiringLease(Network):
        fresh = False

        async def connect(self, candidate):
            result = await super().connect(candidate)
            self.fresh = False
            return result

        async def keep_alive(self):
            self.fresh = True
            return await super().keep_alive()

    async def scenario():
        network = ExpiringLease()
        rescue = runtime(tmp_path, network)
        spawn = rescue.processes.spawn
        freshness = []

        async def launch(*args, **kwargs):
            freshness.append(network.fresh)
            return await spawn(*args, **kwargs)

        rescue.processes.spawn = launch
        await rescue.run(seconds=.3)
        assert freshness == [True], "45-second connection setup can consume most of a 60-second lease"
    asyncio.run(scenario())


def test_expired_lease_reply_never_allows_connect(tmp_path):
    class ExpiredLease(Network):
        async def keep_alive(self):
            return {"api_version": 1, "lease_deadline": time.monotonic() - 1}

    async def scenario():
        rescue = runtime(tmp_path, ExpiredLease())
        with pytest.raises(RescueError, match="attempts_exhausted"):
            await rescue.run(seconds=1)
        assert rescue.network.connects == 0
    asyncio.run(scenario())


def test_repeated_cancellation_waits_for_restore_before_returning(tmp_path):
    class SlowRelease(Network):
        def __init__(self):
            super().__init__()
            self.releasing, self.resume = asyncio.Event(), asyncio.Event()

        async def release(self):
            self.releasing.set()
            await self.resume.wait()
            return await super().release()

    async def scenario():
        network = SlowRelease()
        rescue = runtime(tmp_path, network)
        task = asyncio.create_task(rescue.run(seconds=2))
        await network.entered.wait()
        task.cancel()
        await network.releasing.wait()
        task.cancel()
        await asyncio.sleep(.01)
        assert not task.done()
        network.resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert network.closes == 1
        assert rescue.network_restored is True
        assert not rescue.processes.children
    asyncio.run(scenario())


def test_release_exception_is_sanitized_and_owner_is_closed(tmp_path):
    class BrokenRelease(Network):
        async def release(self):
            raise RuntimeError("private network diagnostic")

    async def scenario():
        rescue = runtime(tmp_path, BrokenRelease())
        rescue.request_stop()
        with pytest.raises(RescueError, match="network_restore_failed"):
            await rescue.run(seconds=1)
        assert rescue.network.closes == 1
        assert rescue.network_restored is False
    asyncio.run(scenario())


def test_link_binding_is_rechecked_before_worker_launch(tmp_path):
    class WrongLink(Network):
        async def connect(self, candidate):
            result = await super().connect(candidate)
            result["peer_address"] = "02:00:00:00:00:02"
            return result

    async def scenario():
        rescue = runtime(tmp_path, WrongLink())
        with pytest.raises(RescueError, match="attempts_exhausted"):
            await rescue.run(seconds=1)
        assert rescue.worker_process is None
        assert rescue.network.releases >= 1
        assert rescue.network.closes == 1
    asyncio.run(scenario())


def test_oversized_or_partial_frame_is_rejected_before_writing():
    async def scenario():
        read_fd, write_fd = os.pipe()
        try:
            for frame in (b"", FRAME[:-1], FRAME + b"x"):
                with pytest.raises(ValueError):
                    await write_frame(write_fd, frame, timeout=.1)
            os.set_blocking(read_fd, False)
            with pytest.raises(BlockingIOError):
                os.read(read_fd, 1)
        finally:
            os.close(read_fd)
            os.close(write_fd)
    asyncio.run(scenario())


@pytest.mark.skipif(importlib.util.find_spec("cairo") is None, reason="Pi pycairo required")
def test_cairo_frame_is_opaque_bgrx_with_visible_counter_and_status():
    renderer = CairoCard()
    first = renderer(0, "connecting")
    second = renderer(1, "active")
    assert len(first) == 1280 * 720 * 4
    assert len(second) == len(first)
    assert first != second
    assert first[:4] == bytes((0, 0, 0, 255))
    pixels = memoryview(first)
    assert sum(pixels[i] > 220 for i in range(0, len(first), 4)) > 10000
    assert all(pixels[i] == pixels[i + 1] == pixels[i + 2] for i in range(0, len(first), 4))
    with pytest.raises(ValueError):
        renderer(2, "private arbitrary log")
