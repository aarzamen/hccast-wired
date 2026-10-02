"""Owned unprivileged media processes; direct Wayland capture, no VNC path."""

import asyncio
from collections import deque
import contextlib
import json
import os
from pathlib import Path
import signal
import time
import math


class ProcessGroup:
    def __init__(self):
        self.children = []
        self._creations = set()
        self._close_task = None

    async def spawn(self, args, **kwargs):
        if self._close_task and not self._close_task.done():
            raise RuntimeError("Process cleanup is still running")

        async def create_owned():
            child = await asyncio.create_subprocess_exec(
                *args,
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
            self.children.append(child)
            return child

        creation = asyncio.create_task(create_owned())
        self._creations.add(creation)
        try:
            return await asyncio.shield(creation)
        except asyncio.CancelledError:
            # The independent creation task takes ownership before returning.
            await self.close()
            raise
        finally:
            if creation.done():
                self._creations.discard(creation)

    async def close(self, grace=3, kill_grace=3):
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close(grace, kill_grace))
        # Cancelling a caller must not cancel cleanup or forget its children.
        await asyncio.shield(self._close_task)

    async def _close(self, grace, kill_grace):
        if self._creations:
            creations = tuple(self._creations)
            await asyncio.gather(*creations, return_exceptions=True)
            self._creations.difference_update(creations)
        children = tuple(self.children)
        for child in children:
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
        failed = []
        for child in children:
            try:
                await asyncio.wait_for(child.wait(), grace)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
                try:
                    await asyncio.wait_for(child.wait(), kill_grace)
                except asyncio.TimeoutError:
                    failed.append(child.pid)
                    continue
            self.children.remove(child)
        if failed:
            raise RuntimeError("Could not reap owned media processes after SIGKILL")


class DesktopMedia:
    def __init__(
        self,
        output,
        worker=Path("/usr/lib/panelbridge/bin/panelbridge-wfd-worker"),
        *,
        continuous=True,
        seconds=60,
        stall_seconds=12,
    ):
        self.output, self.worker = output, str(worker)
        self.continuous, self.seconds = continuous, seconds
        self.processes = ProcessGroup()
        self.reader_tasks = []
        self.ready_event = asyncio.Event()
        self.events = deque(maxlen=120)
        self.errors = deque(maxlen=12)
        self.worker_process = self.capture = None
        self.latest_metrics = None
        self._start_task = self._close_task = self._output_task = None
        if not math.isfinite(stall_seconds) or stall_seconds <= 0:
            raise ValueError("A positive bounded stream stall deadline is required")
        self.stall_seconds = stall_seconds
        self._last_progress = time.monotonic()
        self._encoded_buffers = 0
        self._streaming_state = self._have_frames = False

    async def start(self, link, profile):
        if self._close_task:
            await asyncio.shield(self._close_task)
        if self.processes.children or (self._start_task and not self._start_task.done()):
            raise RuntimeError("Media is already running")
        if os.geteuid() == 0:
            raise RuntimeError("Capture and encoding must never run as root")
        self._start_task = asyncio.create_task(self._start(link, profile))
        try:
            await asyncio.shield(self._start_task)
        except BaseException:
            await self.close()
            raise

    async def _start(self, link, profile):
        self.ready_event.clear()
        self.latest_metrics = None
        self._last_progress = time.monotonic()
        self._encoded_buffers = 0
        self._streaming_state = self._have_frames = False
        self.events.clear()
        self.errors.clear()
        self._output_task = asyncio.create_task(asyncio.to_thread(self.output.apply, profile))
        read_fd = write_fd = None
        try:
            # A cancelled await cannot stop the thread's compositor mutation.
            await asyncio.shield(self._output_task)
            read_fd, write_fd = os.pipe()
            args = [
                self.worker,
                "--source",
                "raw",
                "--fd",
                str(read_fd),
                "--interface",
                link["p2p_interface"],
                "--peer-name",
                link["peer_name"],
                "--peer-address",
                link["peer_address"],
                "--peer-ip",
                link["peer_ip"],
                "--connection-uuid",
                link["connection_uuid"],
                "--connect-timeout",
                "35",
                *profile.worker_arguments(),
            ]
            args += ["--continuous"] if self.continuous else ["--seconds", str(self.seconds)]
            self.worker_process = await self.processes.spawn(args, pass_fds=(read_fd,))
            self.reader_tasks.append(
                asyncio.create_task(self._read_events(self.worker_process.stdout))
            )
            self.reader_tasks.append(
                asyncio.create_task(self._drain_errors(self.worker_process.stderr))
            )
            self.capture = await self.processes.spawn(
                self.output.capture_arguments(profile, write_fd), pass_fds=(write_fd,)
            )
            self.reader_tasks.append(asyncio.create_task(self._drain_errors(self.capture.stderr)))
            self.reader_tasks.append(asyncio.create_task(self._drain_errors(self.capture.stdout)))
        except BaseException:
            await self.processes.close()
            raise
        finally:
            if read_fd is not None:
                os.close(read_fd)
            if write_fd is not None:
                os.close(write_fd)

    async def _read_events(self, stream):
        while line := await stream.readline():
            if len(line) > 32768:
                continue
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if not isinstance(event, dict):
                continue
            self.events.append(event)
            if event.get("event") == "state":
                # WAIT_STREAMING is a distinct native state. A substring match
                # starts profile countdowns before RTSP setup has finished.
                self._streaming_state = event.get("detail") == "ND_SINK_STATE_STREAMING"
            if event.get("event") == "pipeline-telemetry":
                self.latest_metrics = event
                count = event.get("encoded_buffers")
                if type(count) is int and count > self._encoded_buffers:
                    self._encoded_buffers = count
                    self._last_progress = time.monotonic()
                frames, size = event.get("encoded_frames"), event.get("encoded_bytes")
                if (event.get("final") is False
                        and event.get("source_probe_available") is True
                        and event.get("encoder_probes_available") is True
                        and type(frames) is int and frames > 0
                        and type(size) is int and size > 0
                        and event.get("encoded_buffers_with_unknown_frame_count") == 0):
                    self._have_frames = True
            if self._streaming_state and self._have_frames and not self.ready_event.is_set():
                self.ready_event.set()
                self._last_progress = time.monotonic()

    async def _drain_errors(self, stream):
        while line := await stream.readline():
            self.errors.append(line.decode("utf-8", "replace")[:1024])

    async def ready(self):
        ready = asyncio.create_task(self.ready_event.wait())
        ended = asyncio.create_task(self.wait())
        try:
            done, _ = await asyncio.wait(
                [ready, ended], timeout=40, return_when=asyncio.FIRST_COMPLETED
            )
            if ended.done() or any(
                getattr(child, "returncode", None) is not None
                for child in (self.worker_process, self.capture) if child is not None
            ):
                if ended.done():
                    await ended
                raise RuntimeError("The media stream ended before readiness was confirmed")
            if ready not in done:
                raise RuntimeError("The media worker did not reach streaming state")
        finally:
            ready.cancel()
            ended.cancel()
            await asyncio.gather(ready, ended, return_exceptions=True)

    async def wait(self):
        children = [p for p in (self.worker_process, self.capture) if p]
        if not children:
            raise RuntimeError("Media is not running")
        waits = [asyncio.create_task(p.wait()) for p in children]
        waits.append(asyncio.create_task(self._watch_progress()))
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            return next(iter(done)).result()
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def _watch_progress(self):
        while True:
            await asyncio.sleep(min(1, self.stall_seconds / 4))
            if self.ready_event.is_set() and time.monotonic() - self._last_progress >= self.stall_seconds:
                raise RuntimeError("Desktop capture or encoding stopped producing frames")

    async def close(self):
        if self._close_task is None or self._close_task.done():
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._start_task:
            if not self._start_task.done():
                self._start_task.cancel()
            await asyncio.gather(self._start_task, return_exceptions=True)
            self._start_task = None
        # Join the old output application before permitting a new baseline/start.
        # OutputAdapter's actual subprocess calls each carry a finite deadline.
        if self._output_task:
            await asyncio.gather(self._output_task, return_exceptions=True)
            self._output_task = None
        await self.processes.close()
        for task in self.reader_tasks:
            task.cancel()
        await asyncio.gather(*self.reader_tasks, return_exceptions=True)
        self.reader_tasks = []
        self.worker_process = self.capture = None
