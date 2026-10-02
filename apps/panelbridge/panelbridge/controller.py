"""Unprivileged desktop lifecycle; GUI loss never owns the stream or revert timer."""

import asyncio
import contextlib
import math
import time

from .models import Profile
from .credits import about_record


class Controller:
    def __init__(self, store, network, media, *, clock=time.monotonic, retry_delays=(2, 8),
                 heartbeat_timeout=5):
        self.store, self.network, self.media = store, network, media
        self.clock, self.retry_delays = clock, retry_delays
        self.status = "stopped"
        self.message = store.config_error or "Ready to connect"
        self.candidates = []
        self.receiver = None
        self.health = {"temperature_c": None, "cpu_percent": None, "throttled_bits": None}
        self.stream_task = self.operation = None
        self.trial_profile = None
        self.desired = self.resume_after_unlock = False
        self.session_unlocked = True
        self.closed = False
        self.next_heartbeat = 0
        self.stable_since = None
        self._stable_recorded = False
        self._reverting = False
        self._generation = 0
        self._transition_lock = asyncio.Lock()
        self._stream_cleanup = None
        self.heartbeat_task = None
        self.heartbeat_timeout = heartbeat_timeout

    def snapshot(self):
        trial = None
        if self.store.pending:
            trial = {
                "kind": "profile",
                "seconds_remaining": max(
                    0, math.ceil(self.store.pending["deadline"] - self.clock())
                ),
                "message": "Keep only if the panel is readable and moving correctly.",
            }
        elif self.trial_profile:
            trial = {
                "kind": "profile",
                "seconds_remaining": 0,
                "message": "Connecting the trial mode; confirmation starts after streaming.",
            }
        return {
            "api_version": 1,
            "status": self.status,
            "message": self.message,
            "receiver": self.receiver,
            "candidates": [
                {k: p[k] for k in ("id", "name", "manufacturer", "transport") if k in p}
                for p in self.candidates
            ],
            "profile": (self.trial_profile or self.store.effective_profile).to_dict(),
            "presets": {
                k: {
                    "available": False,
                    "reason": "On-device calibration has not completed.",
                    "settings": None,
                    "measurement": None,
                }
                for k in ("recommended", "performance", "battery_saver")
            },
            "health": dict(self.health),
            "pipeline_metrics": getattr(self.media, "latest_metrics", None),
            "trial": trial,
            "features": [
                "discover",
                "start",
                "stop",
                "select",
                "apply_profile",
                "confirm_profile",
                "revert_profile",
                "about",
                "restore_defaults",
            ],
            "feature_reasons": {
                "calibrate": "Calibration integration is in progress.",
                "set_cpu_cap": "CPU policy controls have not been verified.",
                "trial_clock": "Boot recovery must pass before clock trials are enabled.",
                "start_recovery_pairing": "LAN recovery enrollment is not installed yet.",
            },
            "capabilities": {"cpu_caps_mhz": [], "trial_clocks_mhz": []},
            "recovery": {
                "ssh_available": None,
                "lan_enrolled": False,
                "message": "Keep Ethernet connected for existing SSH recovery. USB video is unavailable in v1.",
            },
        }

    def _launch(self, action):
        if self.operation and not self.operation.done():
            raise ValueError("A connection change is already running; Stop remains available.")
        self._generation += 1
        self.operation = asyncio.create_task(self._guard(action, self._generation))

    async def _guard(self, action, generation):
        try:
            await action(generation)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if generation == self._generation:
                self.status, self.message = "error", str(error)

    def _can_stream(self, generation):
        return (generation == self._generation and self.desired and self.session_unlocked
                and not self.closed)

    async def _cancel_operation(self):
        task = self.operation
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.shield(task)
        if self.operation is task:
            self.operation = None

    async def command(self, verb, payload):
        if verb in ("stop", "restore_defaults") and not self.closed and isinstance(payload, dict) and not payload:
            # Invalidate continuations immediately, even if another transition is
            # currently joining cleanup under the lock.
            self.desired = self.resume_after_unlock = False
            self._generation += 1
        async with self._transition_lock:
            return await self._command(verb, payload)

    async def _command(self, verb, payload):
        if self.closed or not isinstance(payload, dict):
            raise ValueError("Controller is closed or command is invalid")
        if verb not in ("select", "apply_profile") and payload:
            raise ValueError("This command takes no arguments")
        if verb == "about":
            return about_record()
        if verb == "stop":
            await self._cancel_operation()
            self._launch(lambda _: self._stop())
        elif verb == "restore_defaults":
            await self._cancel_operation()
            self._launch(lambda _: self._restore_defaults())
        elif verb == "start":
            if not self.session_unlocked:
                raise ValueError("Unlock the normal desktop first")
            if self.store.config_error:
                raise ValueError("Restore the broken configuration first")
            self._launch(self._start)
            self.desired = True
        elif verb == "discover":
            if self.desired:
                raise ValueError("Stop the stream before discovering another receiver")
            self._launch(lambda _: self._discover())
        elif verb == "select":
            if self.desired:
                raise ValueError("Stop before selecting another receiver")
            if set(payload) != {"candidate_id"}:
                raise ValueError("A candidate id is required")
            peer = next((p for p in self.candidates if p["id"] == payload["candidate_id"]), None)
            if not peer:
                raise ValueError("Discover this receiver again before selecting it")
            self.store.select_device(peer["peer_address"], peer["name"])
        elif verb == "apply_profile":
            if self.status != "streaming":
                raise ValueError("Start the monitor before trying a display setting")
            if self.store.pending or self.trial_profile:
                raise ValueError("Keep or revert the current trial first")
            profile = Profile.from_dict(payload)
            if profile.rotation:
                raise ValueError("Rotated capture is not verified yet")
            self._launch(lambda generation: self._change_profile(profile, generation))
        elif verb == "confirm_profile":
            now = self.clock()
            if self.store.pending and now >= self.store.pending["deadline"]:
                # Do not let the store clear the only rollback trigger while the
                # media worker is still using the unconfirmed profile.
                self._launch(self._revert)
                raise ValueError("The display trial expired; restoring the last confirmed setting")
            if self.status != "streaming" or not self.store.confirm_trial(now):
                raise ValueError("No live display trial is available to keep")
            self.trial_profile = None
            self.message = "Display settings saved"
        elif verb == "revert_profile":
            self._generation += 1
            await self._cancel_operation()
            self._launch(self._revert)
        else:
            raise ValueError("This feature is not available in the installed controller")
        return {"accepted": True}

    async def _release(self):
        result = await self.network.release()
        if not result.get("network_restored", False):
            raise RuntimeError("Network restoration needs attention; reconnect is stopped")

    async def _stop_stream(self):
        if self._stream_cleanup is None or self._stream_cleanup.done():
            self._stream_cleanup = asyncio.create_task(self._cleanup_stream())
        await asyncio.shield(self._stream_cleanup)

    async def _cleanup_stream(self):
        await self._stop_heartbeat()
        task = self.stream_task
        if self.stream_task:
            if not task.done():
                task.cancel()
            # A stream's first cleanup attempt may fail. Join it and then retry
            # the still-owned media cleanup rather than repeatedly re-raising
            # the same completed stream exception and losing the retry path.
            await asyncio.gather(task, return_exceptions=True)
            if self.stream_task is task:
                self.stream_task = None
        try:
            await self.media.close()
        finally:
            if task is not None:
                await self._release()

    async def _stop(self, message="Stopped; original network restored"):
        self.desired = False
        await self._stop_stream()
        await self._release()
        if self.store.pending:
            self.store.revert_trial()
        self.trial_profile = None
        self.status, self.message = "stopped", message

    async def _restore_defaults(self):
        await self._stop("Restoring app defaults")
        self.store.restore_defaults()
        self.status = "stopped"
        self.message = "App defaults restored; prior configuration preserved in a private backup"

    async def _start(self, generation):
        await self._stop_stream()
        if not self._can_stream(generation):
            return
        await self._release()  # Explicit user retry rearms the helper's failure guard.
        if self._can_stream(generation):
            self.stream_task = asyncio.create_task(self._stream(generation))

    async def _discover(self):
        self.status, self.message = "discovering", "Looking for available wireless displays"
        try:
            await self._release()
            self.candidates = (await self.network.discover())["candidates"]
            self.status, self.message = (
                "stopped",
                f"Found {len(self.candidates)} available receiver(s)",
            )
        finally:
            await self._release()

    def _choose(self):
        if self.store.selected_device:
            address = self.store.selected_device["address"]
            peer = next((p for p in self.candidates if p["peer_address"].lower() == address), None)
            if peer is None:
                raise RuntimeError(
                    "The selected receiver is absent; another screen will not be substituted"
                )
            return peer
        if len(self.candidates) != 1:
            raise RuntimeError("Select your receiver from the Connection page")
        peer = self.candidates[0]
        self.store.select_device(peer["peer_address"], peer["name"])
        return peer

    async def _stream(self, generation):
        attempts = 0
        while self._can_stream(generation):
            connected = False
            connect_attempted = False
            try:
                self.status, self.message = "discovering", "Finding the selected receiver"
                self.candidates = (await self.network.discover())["candidates"]
                if not self._can_stream(generation):
                    raise asyncio.CancelledError
                peer = self._choose()
                self.status, self.message = "connecting", "Connecting over Wi-Fi Direct"
                connect_attempted = True
                link = await self.network.connect(peer["id"])
                connected = True
                if not self._can_stream(generation):
                    raise asyncio.CancelledError
                profile = self.trial_profile or self.store.profile
                await self.media.start(link, profile)
                if not self._can_stream(generation):
                    raise asyncio.CancelledError
                await self.media.ready()
                if not self._can_stream(generation):
                    raise asyncio.CancelledError
                if self.trial_profile and not self.store.pending:
                    self.store.begin_trial(profile, self.clock())
                self.receiver = {
                    "name": peer["name"],
                    "transport": "wireless",
                    "verification": "Hardware-verified baseline; this application is in development",
                }
                self.status, self.message = (
                    "streaming",
                    "Normal desktop streaming; native panel resolution is unknown",
                )
                self.stable_since = self.clock()
                self._stable_recorded = False
                self.next_heartbeat = 0
                await self.media.wait()
                raise RuntimeError("The display stream ended")
            except asyncio.CancelledError:
                # Release also cancels an in-flight helper operation, even if its
                # thread-backed D-Bus call outlives the awaiting coroutine.
                await self.media.close()
                await self._release()
                raise
            except Exception as error:
                self.message = str(error)
                await self.media.close()
                if connected or not connect_attempted:
                    await self._release()
                attempts += 1
                self.store.record_failure()
                if self.trial_profile or self.store.pending:
                    self.store.revert_trial()
                    self.trial_profile = None
                    self.message = "Trial failed; restoring the last confirmed display setting"
                if attempts >= 3 or self.store.recovery_required:
                    self.status = "error"
                    self.desired = False
                    break
                self.status = "retrying"
                await asyncio.sleep(
                    self.retry_delays[min(attempts - 1, len(self.retry_delays) - 1)]
                )
            finally:
                self.stable_since = None
        # Discovery errors are not always Connect failures, so release any lease.
        await self._release()

    async def _change_profile(self, profile, generation):
        await self._stop_stream()
        if self._can_stream(generation):
            self.trial_profile = profile
            self.stream_task = asyncio.create_task(self._stream(generation))

    async def _revert(self, generation):
        self._reverting = True
        try:
            await self._stop_stream()
            if self.store.pending:
                self.store.revert_trial()
            self.trial_profile = None
            self.message = "Restoring the last confirmed display setting"
            if self._can_stream(generation):
                self.stream_task = asyncio.create_task(self._stream(generation))
        finally:
            self._reverting = False

    async def tick(self):
        if self.closed:
            return
        if (
            self.store.pending
            and self.clock() >= self.store.pending["deadline"]
            and not self._reverting
            and (self.operation is None or self.operation.done())
            and not self._transition_lock.locked()
        ):
            self._launch(self._revert)
        if (self.status == "streaming" and self.clock() >= self.next_heartbeat
                and self.heartbeat_task is None and self.desired and self.session_unlocked
                and (self.operation is None or self.operation.done())):
            self.next_heartbeat = self.clock() + 10
            self.heartbeat_task = asyncio.create_task(self._heartbeat(self._generation))
        if (
            self.status == "streaming"
            and self.stable_since is not None
            and self.clock() - self.stable_since >= 30
            and not self._stable_recorded
        ):
            self.store.record_success()
            self._stable_recorded = True

    async def session_changed(self, active):
        if active == self.session_unlocked:
            return
        self.session_unlocked = active
        if not active:
            self.resume_after_unlock = self.desired
            self.desired = False
            self._generation += 1
            async with self._transition_lock:
                await self._cancel_operation()
                await self._stop("Desktop locked or unavailable; capture is paused")
        else:
            async with self._transition_lock:
                if self.resume_after_unlock and self.session_unlocked and not self.closed:
                    self.resume_after_unlock = False
                    await self._cancel_operation()
                    self._launch(self._start)
                    self.desired = True

    async def _heartbeat(self, generation):
        try:
            await asyncio.wait_for(self.network.keep_alive(), self.heartbeat_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._can_stream(generation):
                self.desired = self.resume_after_unlock = False
                self._generation += 1
                async with self._transition_lock:
                    await self._cancel_operation()
                    self._launch(lambda _: self._stop(
                        "Network lease was lost; stopped capture and restored the network"))
        finally:
            if self.heartbeat_task is asyncio.current_task():
                self.heartbeat_task = None

    async def _stop_heartbeat(self):
        task = self.heartbeat_task
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            if self.heartbeat_task is task:
                self.heartbeat_task = None

    async def close(self):
        self.closed = True
        self.desired = self.resume_after_unlock = False
        self._generation += 1
        async with self._transition_lock:
            await self._cancel_operation()
            await self._stop_stream()
            await self._release()
