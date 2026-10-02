"""Unprivileged, desktop-independent rescue card and bounded WFD runtime.

The integrator supplies the helper client, dedicated UID and health sampler.
This module neither opens a desktop nor implements authenticated controls.
"""

import asyncio
from dataclasses import dataclass, fields
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import uuid

from .media import ProcessGroup
from .models import Profile


WIDTH, HEIGHT = 1280, 720
FRAME_BYTES = WIDTH * HEIGHT * 4
PROFILE = Profile(content_fps=5)
CARD_STATUS = {
    "connecting": "CONNECTING RECOVERY DISPLAY",
    "active": "RECOVERY DISPLAY ACTIVE",
    "waiting": "WAITING FOR AUTHORIZED RECOVERY",
}


class RescueError(RuntimeError):
    """Fixed error code; never a receiver log, credential or arbitrary text."""


@dataclass(frozen=True)
class RescueTimeouts:
    discover: float = 30
    connect: float = 60
    startup: float = 40
    heartbeat: float = 8
    heartbeat_interval: float = 15
    health: float = 3
    health_interval: float = 1
    stall: float = 12
    write: float = 3
    cleanup: float = 100
    retry_delay: float = 2
    terminate: float = 3
    kill: float = 3

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if (type(value) not in (int, float) or not math.isfinite(value)
                    or not 0 < value <= field.default):
                raise ValueError(f"{field.name} must be positive and no larger than its default")


class CairoCard:
    """1280x720 little-endian BGRx pixels, cached while the card is unchanged."""

    def __init__(self):
        if sys.byteorder != "little":
            raise RescueError("unsupported_pixel_byte_order")
        try:
            import cairo
        except ImportError:
            raise RescueError("pycairo_unavailable") from None
        self.cairo = cairo
        self._key = self._frame = None

    def __call__(self, seconds, status):
        if type(seconds) is not int or not 0 <= seconds <= 14400 or status not in CARD_STATUS:
            raise ValueError("A bounded seconds count and fixed card status are required")
        key = (seconds, status)
        if key == self._key:
            return self._frame
        cairo = self.cairo
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, WIDTH, HEIGHT)
        context = cairo.Context(surface)
        context.set_source_rgb(0, 0, 0)
        context.paint()
        context.set_source_rgb(1, 1, 1)
        context.set_line_width(4)
        context.rectangle(32, 32, 1216, 656)
        context.stroke()
        context.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)

        def label(text, x, y, size):
            context.set_font_size(size)
            context.move_to(x, y)
            context.show_text(text)

        label("PANELBRIDGE  /  RECOVERY", 72, 110, 40)
        context.rectangle(72, 140, 1136, 4)
        context.fill()
        label(CARD_STATUS[status], 72, 222, 38)
        label(f"{seconds:05d} s", 72, 360, 96)
        # Moving shape and seconds are visible progress markers, not delivery proof.
        context.rectangle(1020 + (seconds % 2) * 70, 278, 56, 56)
        context.fill()
        context.select_font_face("sans-serif", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        label("Desktop recovery is required.", 72, 452, 36)
        label("Keep Ethernet and power connected.", 72, 512, 34)
        label("Use an already authorized recovery connection.", 72, 566, 32)
        label("This display does not accept keyboard or mouse input.", 72, 634, 26)
        surface.flush()
        if surface.get_stride() != WIDTH * 4:
            raise RescueError("unsupported_pixel_stride")
        self._frame = bytes(surface.get_data())
        self._key = key
        surface.finish()
        return self._frame


async def write_frame(fd, frame, *, timeout):
    """Write one complete frame without blocking the loop or queuing more frames."""
    if not isinstance(frame, bytes) or len(frame) != FRAME_BYTES:
        raise ValueError("The raw worker requires one complete 1280x720 BGRx frame")
    os.set_blocking(fd, False)
    loop = asyncio.get_running_loop()
    offset = 0
    async with asyncio.timeout(timeout):
        while offset < len(frame):
            try:
                count = os.write(fd, memoryview(frame)[offset:offset + 65536])
                if not count:
                    raise BrokenPipeError("Rescue pipe stopped accepting bytes")
                offset += count
            except BlockingIOError:
                writable = loop.create_future()

                def ready():
                    if not writable.done():
                        writable.set_result(None)

                loop.add_writer(fd, ready)
                try:
                    await writable
                finally:
                    loop.remove_writer(fd)


def _health_bits(sample, baseline=None):
    if not isinstance(sample, dict):
        raise RescueError("health_unknown")
    temperature, bits = sample.get("temperature_c"), sample.get("throttled_bits")
    if (type(temperature) not in (int, float) or not math.isfinite(temperature)
            or not -20 <= temperature < 70 or type(bits) is not int
            or bits < 0 or bits & ~0xF000F or bits & 0xF):
        raise RescueError("health_unsafe_or_unknown")
    if baseline is not None and bits & 0xF0000 & ~baseline:
        raise RescueError("health_new_fault")
    return bits


class RescueRuntime:
    """One-use runtime owning one helper connection, worker, pipe and card.

    `health` is an async callable returning HealthMonitor-shaped samples. Its
    implementation must be cancellation-responsive and own any subprocesses.
    `network.close()` must disconnect its unique helper owner and join pending
    network operations, including ones whose callers were cancelled.
    """

    def __init__(self, network, *, receiver_address, expected_uid, health,
                 worker=Path("/usr/lib/panelbridge/bin/panelbridge-wfd-worker"),
                 renderer=None, timeouts=RescueTimeouts()):
        if (not isinstance(receiver_address, str)
                or not re.fullmatch(r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}", receiver_address)
                or int(receiver_address[:2], 16) & 1):
            raise ValueError("An exact saved unicast receiver address is required")
        if type(expected_uid) is not int or expected_uid <= 0:
            raise ValueError("A dedicated nonroot UID is required")
        if not Path(worker).is_absolute():
            raise ValueError("Use the installed worker's absolute path")
        self.network, self.health = network, health
        self.receiver_address, self.expected_uid = receiver_address.lower(), expected_uid
        self.worker, self.renderer, self.timeouts = str(worker), renderer, timeouts
        self.processes = ProcessGroup()
        self.worker_process = None
        self.status = "stopped"
        self.attempts = self.frames_written = 0
        self.network_restored = False
        self._stop = asyncio.Event()
        self._started = False
        self._tasks, self._media_tasks = [], []
        self._write_fd = None

    def request_stop(self):
        self._stop.set()

    async def run(self, *, seconds=600):
        if (type(seconds) not in (int, float) or not math.isfinite(seconds)
                or not 0 < seconds <= 14400):
            raise ValueError("A finite rescue duration of at most four hours is required")
        if os.getuid() != self.expected_uid or os.geteuid() != self.expected_uid:
            raise RescueError("dedicated_identity_required")
        if self._started:
            raise RescueError("runtime_already_used")
        self._started = True
        self._began = time.monotonic()
        reason = "requested"
        try:
            self.renderer = self.renderer or CairoCard()
            baseline = _health_bits(await self._sample_health())
            self._tasks = [asyncio.create_task(self._attempts()),
                           asyncio.create_task(self._watch_health(baseline)),
                           asyncio.create_task(self._stop.wait())]
            done, _ = await asyncio.wait(self._tasks, timeout=seconds,
                                         return_when=asyncio.FIRST_COMPLETED)
            # Health/transport errors take precedence over a simultaneous stop.
            for task in self._tasks[:2]:
                if task in done:
                    task.result()
            if not done:
                reason = "duration_limit"
        finally:
            self.status = "stopping"
            await self._finish(self._cleanup())
            self.status = "stopped"
        return {"status": self.status, "reason": reason, "attempts": self.attempts,
                "frames_written": self.frames_written, "network_restored": self.network_restored}

    @staticmethod
    async def _finish(cleanup):
        # Repeated caller cancellation cannot orphan the shared cleanup operation.
        task = asyncio.create_task(cleanup)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        task.result()
        if cancelled:
            raise asyncio.CancelledError

    async def _sample_health(self):
        try:
            return await asyncio.wait_for(self.health(), self.timeouts.health)
        except Exception:
            raise RescueError("health_unavailable") from None

    async def _watch_health(self, baseline):
        while True:
            await asyncio.sleep(self.timeouts.health_interval)
            _health_bits(await self._sample_health(), baseline)

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(self.timeouts.heartbeat_interval)
            await self._renew()

    async def _renew(self):
        reply = await asyncio.wait_for(self.network.keep_alive(), self.timeouts.heartbeat)
        deadline = reply.get("lease_deadline") if isinstance(reply, dict) else None
        if (not isinstance(reply, dict) or reply.get("api_version") != 1
                or type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic() + self.timeouts.heartbeat_interval
                + self.timeouts.heartbeat):
            raise RescueError("lease_renewal_failed")

    def _select(self, discovery):
        candidates = discovery.get("candidates") if isinstance(discovery, dict) else None
        if not isinstance(candidates, list) or len(candidates) > 64:
            raise RescueError("invalid_discovery")
        matches = [candidate for candidate in candidates if isinstance(candidate, dict)
                   and str(candidate.get("peer_address", "")).lower() == self.receiver_address]
        if len(matches) != 1 or matches[0].get("wfd_available") is not True:
            raise RescueError("selected_receiver_unavailable")
        candidate_id = matches[0].get("id")
        if not isinstance(candidate_id, str) or not 1 <= len(candidate_id) <= 128:
            raise RescueError("invalid_candidate")
        return candidate_id

    def _arguments(self, link, read_fd):
        try:
            if link["peer_address"].lower() != self.receiver_address:
                raise ValueError("binding")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", link["p2p_interface"]):
                raise ValueError("interface")
            if not re.fullmatch(r"EBPS[I1]-[A-Za-z0-9]+", link["peer_name"]):
                raise ValueError("receiver")
            ip = ipaddress.IPv4Address(link["peer_ip"])
            if ip.is_multicast or ip.is_unspecified or ip.is_loopback:
                raise ValueError("address")
            uuid.UUID(link["connection_uuid"])
        except (KeyError, TypeError, ValueError, AttributeError):
            raise RescueError("invalid_selected_link") from None
        return [self.worker, "--source", "raw", "--fd", str(read_fd),
                "--interface", link["p2p_interface"], "--peer-name", link["peer_name"],
                "--peer-address", link["peer_address"], "--peer-ip", link["peer_ip"],
                "--connection-uuid", link["connection_uuid"], "--connect-timeout", "35",
                *PROFILE.worker_arguments(), "--continuous"]

    async def _attempts(self):
        for attempt in range(1, 4):
            self.attempts = attempt
            self.status = "connecting"
            connected = False
            try:
                discovery = await asyncio.wait_for(self.network.discover(), self.timeouts.discover)
                candidate = self._select(discovery)
                # Refresh after discovery so its time is not deducted from connection setup.
                await self._renew()
                link = await asyncio.wait_for(self.network.connect(candidate), self.timeouts.connect)
                connected = True
                # Setup can consume 45 seconds of the helper's 60-second lease.
                # Renew now rather than waiting for the first periodic heartbeat.
                await self._renew()
                await self._stream(link)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.status = "retrying"
            finally:
                await self._finish(self._cleanup_media())
            if connected:
                await self._release()
            # Connect failures already restore in the helper. Do not reset its
            # failure counter with Release between those failures.
            if attempt < 3:
                await asyncio.sleep(self.timeouts.retry_delay * attempt)
        raise RescueError("attempts_exhausted")

    async def _stream(self, link):
        read_fd, self._write_fd = os.pipe()
        self._ready = asyncio.Event()
        self._progress = time.monotonic()
        self._encoded = 0
        try:
            self.worker_process = await self.processes.spawn(
                self._arguments(link, read_fd), pass_fds=(read_fd,),
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"},
            )
        finally:
            os.close(read_fd)
        self._media_tasks = [
            asyncio.create_task(self._read_events()),
            asyncio.create_task(self._discard_errors()),
            asyncio.create_task(self._produce()),
            asyncio.create_task(self.worker_process.wait()),
            asyncio.create_task(self._watch_stream()),
            asyncio.create_task(self._heartbeat()),
        ]
        done, _ = await asyncio.wait(self._media_tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        raise RescueError("worker_ended")

    async def _read_events(self):
        while line := await self.worker_process.stdout.readline():
            if len(line) > 32768:
                raise RescueError("worker_event_too_large")
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            if event.get("event") == "state" and event.get("detail") == "ND_SINK_STATE_STREAMING":
                self._ready.set()
                self.status = "active"
                self._progress = time.monotonic()
            elif event.get("event") == "pipeline-telemetry":
                count = event.get("encoded_buffers")
                if type(count) is int and count > self._encoded:
                    self._encoded = count
                    self._progress = time.monotonic()

    async def _discard_errors(self):
        while await self.worker_process.stderr.read(4096):
            pass  # Drain bounded chunks without retaining or displaying arbitrary logs.

    async def _produce(self):
        while True:
            seconds = min(14400, int(time.monotonic() - self._began))
            status = "active" if self._ready.is_set() else "connecting"
            frame = self.renderer(seconds, status)
            timeout = self.timeouts.write if self._ready.is_set() else self.timeouts.startup
            await write_frame(self._write_fd, frame, timeout=timeout)
            self.frames_written += 1
            await asyncio.sleep(1 / PROFILE.content_fps)

    async def _watch_stream(self):
        await asyncio.wait_for(self._ready.wait(), self.timeouts.startup)
        while True:
            await asyncio.sleep(min(1, self.timeouts.stall / 4))
            if time.monotonic() - self._progress >= self.timeouts.stall:
                raise RescueError("encoder_stalled")

    async def _cleanup_media(self):
        for task in self._media_tasks:
            task.cancel()
        await asyncio.gather(*self._media_tasks, return_exceptions=True)
        self._media_tasks = []
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None
        await self.processes.close(grace=self.timeouts.terminate, kill_grace=self.timeouts.kill)

    async def _release(self):
        self.network_restored = False
        try:
            reply = await asyncio.wait_for(self.network.release(), self.timeouts.cleanup)
        except Exception:
            raise RescueError("network_restore_failed") from None
        self.network_restored = isinstance(reply, dict) and reply.get("network_restored") is True
        if not self.network_restored:
            raise RescueError("network_restore_failed")

    async def _cleanup(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        try:
            await self._cleanup_media()
        finally:
            try:
                await self._release()
            finally:
                try:
                    await asyncio.wait_for(self.network.close(), self.timeouts.cleanup)
                except Exception:
                    self.network_restored = False
                    raise RescueError("network_close_failed") from None
