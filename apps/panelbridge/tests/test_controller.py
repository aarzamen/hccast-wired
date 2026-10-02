"""Lifecycle checks use deterministic substitutes only for external resources."""

import asyncio
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.controller import Controller
from panelbridge.models import Profile
from panelbridge.state import StateStore

PEER = {
    "id": "one",
    "name": "EBPSI-TEST",
    "peer_address": "02:00:00:00:00:01",
    "transport": "wireless",
}


class Network:
    def __init__(self):
        self.peers = [PEER.copy()]
        self.releases = 0
        self.connects = 0
        self.fail = False

    async def discover(self):
        return {"candidates": self.peers}

    async def connect(self, candidate):
        self.connects += 1
        if self.fail:
            raise RuntimeError("Receiver unavailable")
        return {"peer_name": PEER["name"], "peer_address": PEER["peer_address"]}

    async def release(self):
        self.releases += 1
        return {"network_restored": True}

    async def keep_alive(self):
        return {}

    async def session_active(self):
        return True


class Media:
    def __init__(self):
        self.runs = []
        self.current = None
        self.stops = 0

    async def start(self, link, profile):
        self.current = asyncio.get_running_loop().create_future()
        self.runs.append(profile)

    async def ready(self):
        return True

    async def wait(self):
        await self.current

    async def close(self):
        self.stops += 1
        if self.current and not self.current.done():
            self.current.cancel()


async def settle(predicate, count=300):
    for _ in range(count):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("Controller did not settle")


def setup(tmp_path, clock=lambda: 100):
    net, media = Network(), Media()
    return (
        Controller(
            StateStore(tmp_path / "state.json"),
            net,
            media,
            clock=clock,
            retry_delays=(0.001, 0.001),
        ),
        net,
        media,
    )


def test_start_selects_only_unambiguous_first_receiver_and_stop_restores(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        await c.command("start", {})
        await settle(lambda: c.status == "streaming")
        assert c.store.selected_device["address"] == PEER["peer_address"]
        assert c.snapshot()["receiver"]["name"] == PEER["name"]
        await c.command("stop", {})
        await settle(lambda: c.status == "stopped")
        assert n.releases >= 1 and m.stops >= 1
        await c.close()

    asyncio.run(scenario())


def test_bound_receiver_is_never_replaced_by_another_sole_candidate(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        c.store.select_device("02:00:00:00:00:ff", "EBPSI-OWNED")
        await c.command("start", {})
        await settle(lambda: c.status == "error")
        assert n.connects == 0 and not m.runs
        assert c.store.selected_device["name"] == "EBPSI-OWNED"
        await c.close()

    asyncio.run(scenario())


def test_restore_defaults_closes_stream_and_preserves_previous_settings(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        await c.command("start", {})
        await settle(lambda: c.status == "streaming")
        before = c.store.path.read_bytes()
        await c.command("restore_defaults", {})
        await settle(lambda: c.operation.done())
        assert c.status == "stopped" and not c.desired
        assert m.current.cancelled()
        assert list(tmp_path.glob("state.json.before-reset-*"))[0].read_bytes() == before
        assert c.store.selected_device["address"] == PEER["peer_address"]
        await c.close()
    asyncio.run(scenario())


def test_restore_defaults_recovers_broken_config_without_starting_capture(tmp_path):
    async def scenario():
        path = tmp_path / "state.json"
        path.write_text("{broken")
        c, n, m = setup(tmp_path)
        await c.command("restore_defaults", {})
        await settle(lambda: c.operation.done())
        assert c.store.config_error is None and c.status == "stopped"
        assert not m.runs and not n.connects
        assert StateStore(path).config_error is None
        await c.close()
    asyncio.run(scenario())


def test_unconfirmed_mode_reverts_without_gui_and_keep_persists(tmp_path):
    async def scenario():
        now = [100]
        c, n, m = setup(tmp_path, clock=lambda: now[0])
        await c.command("start", {})
        await settle(lambda: c.status == "streaming")
        changed = Profile(content_fps=15)
        await c.command("apply_profile", changed.to_dict())
        await settle(lambda: c.store.pending is not None and c.status == "streaming")
        assert json.loads(c.store.path.read_text())["profile"] == Profile().to_dict()
        now[0] = 121
        await c.tick()
        await settle(lambda: c.status == "streaming" and m.runs[-1] == Profile())
        assert c.store.pending is None
        await c.command("apply_profile", changed.to_dict())
        await settle(lambda: c.store.pending is not None)
        await c.command("confirm_profile", {})
        assert StateStore(c.store.path).profile == changed
        await c.close()

    asyncio.run(scenario())


def test_three_failed_starts_stop_and_keep_last_good_profile(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        n.fail = True
        await c.command("start", {})
        await settle(lambda: c.status == "error" and c.store.failures == 3)
        assert n.connects == 3 and not m.runs
        assert c.store.profile == c.store.known_good
        await c.close()

    asyncio.run(scenario())


def test_lost_session_stops_capture_and_requires_unlock_before_restart(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        await c.command("start", {})
        await settle(lambda: c.status == "streaming")
        await c.session_changed(False)
        await settle(lambda: c.status == "stopped")
        assert m.stops and c.snapshot()["message"].startswith("Desktop locked")
        await c.session_changed(True)
        await settle(lambda: c.status == "streaming" and len(m.runs) == 2)
        await c.close()

    asyncio.run(scenario())


def test_lock_invalidates_start_waiting_for_initial_network_release(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        entered, resume = asyncio.Event(), asyncio.Event()
        release = n.release

        async def gated_release():
            if not entered.is_set():
                entered.set()
                await resume.wait()
            return await release()

        n.release = gated_release
        try:
            await c.command("start", {})
            await entered.wait()
            await c.session_changed(False)
            resume.set()
            for _ in range(12):
                await asyncio.sleep(0)
            assert not m.runs
            assert not c.desired and c.status == "stopped"
        finally:
            resume.set()
            await c.close()

    asyncio.run(scenario())


def test_lock_during_profile_cleanup_never_restarts_until_unlock(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        entered, resume = asyncio.Event(), asyncio.Event()
        close = m.close
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")

            async def gated_close():
                if not entered.is_set():
                    entered.set()
                    await resume.wait()
                await close()

            m.close = gated_close
            await c.command("apply_profile", Profile(content_fps=15).to_dict())
            await entered.wait()
            locking = asyncio.create_task(c.session_changed(False))
            await asyncio.sleep(0)
            resume.set()
            await locking
            for _ in range(12):
                await asyncio.sleep(0)
            assert len(m.runs) == 1
            assert not c.desired and c.stream_task is None
            await c.session_changed(True)
            await settle(lambda: c.status == "streaming" and len(m.runs) == 2)
            assert m.runs[-1] == Profile()
        finally:
            resume.set()
            await c.close()

    asyncio.run(scenario())


def test_stop_during_lock_cleanup_prevents_unlock_from_resuming(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        entered, resume = asyncio.Event(), asyncio.Event()
        close = m.close
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")

            async def gated_close():
                if not entered.is_set():
                    entered.set()
                    await resume.wait()
                await close()

            m.close = gated_close
            locking = asyncio.create_task(c.session_changed(False))
            await entered.wait()
            stopping = asyncio.create_task(c.command("stop", {}))
            unlocking = asyncio.create_task(c.session_changed(True))
            await asyncio.sleep(0)
            resume.set()
            await asyncio.gather(locking, stopping, unlocking)
            await settle(lambda: c.status == "stopped")
            assert len(m.runs) == 1 and not c.desired and not c.resume_after_unlock
        finally:
            resume.set()
            await c.close()

    asyncio.run(scenario())


def test_keep_at_deadline_rejects_and_restores_running_profile(tmp_path):
    async def scenario():
        now = [100]
        c, n, m = setup(tmp_path, clock=lambda: now[0])
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            await c.command("apply_profile", Profile(content_fps=15).to_dict())
            await settle(lambda: c.store.pending is not None and c.status == "streaming")
            now[0] = 120
            with pytest.raises(ValueError):
                await c.command("confirm_profile", {})
            await c.tick()
            await settle(lambda: c.status == "streaming" and m.runs[-1] == Profile())
            assert c.store.pending is None and c.trial_profile is None
        finally:
            await c.close()

    asyncio.run(scenario())


def test_keep_uses_one_timestamp_when_clock_crosses_deadline(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            trial = Profile(content_fps=15)
            await c.command("apply_profile", trial.to_dict())
            await settle(lambda: c.store.pending is not None and c.status == "streaming")
            deadline = c.store.pending["deadline"]
            samples = iter((deadline - 0.000001, deadline + 0.000001))
            c.clock = lambda: next(samples, deadline + 0.000001)
            # The acceptance decision begins before expiry. Its single timestamp
            # must also be used by the store so the running trial is saved.
            await c.command("confirm_profile", {})
            assert c.store.pending is None and c.trial_profile is None
            assert c.store.profile == trial and m.runs[-1] == trial
            assert StateStore(c.store.path).profile == trial
        finally:
            await c.close()

    asyncio.run(scenario())


def test_blocked_heartbeat_does_not_delay_trial_rollback_and_is_joined(tmp_path):
    async def scenario():
        now = [100]
        c, n, m = setup(tmp_path, clock=lambda: now[0])
        entered, released = asyncio.Event(), asyncio.Event()
        closed = asyncio.Event()

        async def blocked_heartbeat():
            entered.set()
            try:
                await released.wait()
            finally:
                closed.set()

        n.keep_alive = blocked_heartbeat
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            await c.command("apply_profile", Profile(content_fps=15).to_dict())
            await settle(lambda: c.store.pending is not None and c.status == "streaming")
            await asyncio.wait_for(c.tick(), 0.1)
            await entered.wait()
            now[0] = 121
            await asyncio.wait_for(c.tick(), 0.1)
            await settle(lambda: c.status == "streaming" and m.runs[-1] == Profile())
            assert closed.is_set()
        finally:
            released.set()
            await c.close()

    asyncio.run(scenario())


def test_stop_retries_cleanup_after_stream_cleanup_failure(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        close = m.close
        failed = False

        async def failed_once():
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("First process cleanup failed")
            await close()

        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            m.close = failed_once
            await c.command("stop", {})
            await settle(lambda: c.status == "stopped")
            assert c.stream_task is None and m.current.done()
        finally:
            m.close = close
            await c.close()

    asyncio.run(scenario())


def test_invalid_stop_payload_cannot_silently_invalidate_live_stream(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            with pytest.raises(ValueError):
                await c.command("stop", {"unexpected": True})
            assert c.desired and c.status == "streaming"
        finally:
            await c.close()

    asyncio.run(scenario())


def test_heartbeat_timeout_stops_capture_without_waiting_for_next_tick(tmp_path):
    async def scenario():
        c, n, m = setup(tmp_path)
        c.heartbeat_timeout = 0.01

        async def stalled():
            await asyncio.Event().wait()

        n.keep_alive = stalled
        try:
            await c.command("start", {})
            await settle(lambda: c.status == "streaming")
            await c.tick()
            await settle(lambda: c.status == "stopped")
            assert not c.desired and c.heartbeat_task is None and m.current.done()
        finally:
            await c.close()

    asyncio.run(scenario())
