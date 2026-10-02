"""Process ownership tests exercise real tiny children without desktop/network."""

import asyncio
import contextlib
import json
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.media import DesktopMedia, ProcessGroup
from panelbridge.models import Profile


def test_close_reaps_only_owned_process_even_when_child_ignores_term():
    async def scenario():
        group = ProcessGroup()
        child = await group.spawn(
            [
                sys.executable,
                "-c",
                'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)',
            ]
        )
        assert await child.stdout.readline() == b"ready\n"
        await group.close(grace=0.02)
        assert child.returncode is not None
        await group.close()

    asyncio.run(scenario())


def test_partial_start_failure_keeps_previous_child_owned_for_cleanup():
    async def scenario():
        group = ProcessGroup()
        child = await group.spawn([sys.executable, "-c", "import time;time.sleep(60)"])
        try:
            await group.spawn(["/nonexistent/panelbridge-fixture"])
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("missing executable should fail")
        await group.close(grace=0.02)
        assert child.returncode is not None

    asyncio.run(scenario())


def test_cancelled_close_retains_children_until_shared_cleanup_reaps_them():
    async def scenario():
        group = ProcessGroup()
        child = await group.spawn([
            sys.executable, "-c", 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); '
            'print("ready",flush=True); time.sleep(60)',
        ])
        await child.stdout.readline()
        try:
            closing = asyncio.create_task(group.close(grace=0.05))
            await asyncio.sleep(0.01)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert child in group.children
            await asyncio.gather(group.close(grace=0.02), group.close(grace=0.02))
            assert child.returncode is not None
            assert not group.children
        finally:
            if child.returncode is None:
                child.kill()
            await child.wait()

    asyncio.run(scenario())


def test_output_apply_thread_finishes_before_cancelled_media_cleanup_returns():
    class Output:
        def __init__(self):
            self.entered = threading.Event()
            self.resume = threading.Event()
            self.current = None

        def apply(self, profile):
            if profile.content_fps == 15:
                self.entered.set()
                assert self.resume.wait(2)
            self.current = profile

    async def scenario():
        output = Output()
        media = DesktopMedia(output)
        start = asyncio.create_task(media.start({}, Profile(content_fps=15)))
        try:
            assert await asyncio.to_thread(output.entered.wait, 1)
            start.cancel()
            closing = asyncio.create_task(media.close())
            for _ in range(5):
                await asyncio.sleep(0)
            assert not closing.done(), "Cleanup must retain the pending output mutation"
            output.resume.set()
            with contextlib.suppress(asyncio.CancelledError):
                await start
            await closing
            await asyncio.to_thread(output.apply, Profile())
            assert output.current == Profile()
            assert not media.processes.children
        finally:
            output.resume.set()
            await asyncio.gather(start, return_exceptions=True)
            await media.close()

    asyncio.run(scenario())


def test_cleanup_has_finite_post_kill_wait_and_retains_unreaped_child(monkeypatch):
    class Unreaped:
        pid = 123
        returncode = None

        async def wait(self):
            await asyncio.Event().wait()

    async def scenario():
        group = ProcessGroup()
        child = Unreaped()
        group.children.append(child)
        monkeypatch.setattr("panelbridge.media.os.killpg", lambda *_: None)
        with pytest.raises(RuntimeError, match="reap"):
            await asyncio.wait_for(group.close(grace=0.01, kill_grace=0.01), 0.2)
        assert child in group.children

    asyncio.run(scenario())


def test_runtime_pipeline_telemetry_replaces_latest_metrics():
    async def scenario():
        media = DesktopMedia(None)
        stream = asyncio.StreamReader()
        stream.feed_data(b'{"event":"pipeline-telemetry","frames":12}\n')
        stream.feed_eof()
        await media._read_events(stream)
        assert media.latest_metrics == {"event": "pipeline-telemetry", "frames": 12}

    asyncio.run(scenario())


def frame_event(**changes):
    return {"event": "pipeline-telemetry", "final": False,
            "source_probe_available": True, "encoder_probes_available": True,
            "encoded_frames": 1, "encoded_buffers": 1, "encoded_bytes": 512,
            "encoded_buffers_with_unknown_frame_count": 0, **changes}


async def feed_events(media, *events):
    stream = asyncio.StreamReader()
    for event in events:
        stream.feed_data((json.dumps(event) + "\n").encode())
    stream.feed_eof()
    await media._read_events(stream)


def test_wait_streaming_does_not_start_a_trial_even_with_encoded_data():
    async def scenario():
        media = DesktopMedia(None)
        await feed_events(media, {"event": "state", "detail": "ND_SINK_STATE_WAIT_STREAMING"},
                          frame_event())
        assert not media.ready_event.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("frames_first", [True, False])
def test_readiness_requires_exact_state_and_positive_frame_evidence(frames_first):
    async def scenario():
        media = DesktopMedia(None)
        state = {"event": "state", "detail": "ND_SINK_STATE_STREAMING"}
        first, second = (frame_event(), state) if frames_first else (state, frame_event())
        await feed_events(media, first)
        assert not media.ready_event.is_set()
        await feed_events(media, second)
        assert media.ready_event.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"final": True}, {"source_probe_available": False}, {"encoder_probes_available": False},
    {"encoded_frames": None}, {"encoded_frames": 0}, {"encoded_frames": True},
    {"encoded_bytes": 0}, {"encoded_buffers_with_unknown_frame_count": 1},
])
def test_incomplete_or_terminal_frame_evidence_cannot_mark_ready(changes):
    async def scenario():
        media = DesktopMedia(None)
        await feed_events(media, {"event": "state", "detail": "ND_SINK_STATE_STREAMING"},
                          frame_event(**changes))
        assert not media.ready_event.is_set()
    asyncio.run(scenario())


def test_ended_process_wins_if_ready_arrives_in_the_same_turn():
    class Ended:
        returncode = 1

        async def wait(self):
            return 1

    async def scenario():
        media = DesktopMedia(None)
        media.worker_process = Ended()
        media.ready_event.set()
        with pytest.raises(RuntimeError, match="ended"):
            await media.ready()
    asyncio.run(scenario())


def test_stalled_encoder_fails_while_child_remains_alive():
    class Alive:
        async def wait(self):
            await asyncio.Event().wait()

    async def scenario():
        media = DesktopMedia(None, stall_seconds=.03)
        media.worker_process = Alive()
        media.ready_event.set()
        with pytest.raises(RuntimeError, match="producing frames"):
            await asyncio.wait_for(media.wait(), .3)
    asyncio.run(scenario())


def test_live_encoder_progress_keeps_stall_guard_healthy():
    class Alive:
        async def wait(self):
            await asyncio.Event().wait()

    async def scenario():
        media = DesktopMedia(None, stall_seconds=.05)
        media.worker_process = Alive()
        media.ready_event.set()
        stream = asyncio.StreamReader()
        reading = asyncio.create_task(media._read_events(stream))
        waiting = asyncio.create_task(media.wait())
        try:
            for count in range(1, 9):
                stream.feed_data(('{"event":"pipeline-telemetry","encoded_buffers":%d}\n' % count).encode())
                await asyncio.sleep(.02)
                assert not waiting.done()
        finally:
            stream.feed_eof()
            waiting.cancel()
            await asyncio.gather(waiting, reading, return_exceptions=True)
    asyncio.run(scenario())
