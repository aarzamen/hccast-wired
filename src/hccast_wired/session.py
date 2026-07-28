"""HCCAST control handshake and video streaming session."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from .annexb import iter_h264_packets, iter_h264_packets_from_stream
from .protocol import (
    Command,
    DeviceInfo,
    Frame,
    FrameCodec,
    FrameStreamParser,
    ScreenInfo,
    Settings,
    make_sets,
    make_setr,
    make_sinf,
    make_vid,
    parse_setv,
)
from .transport import Transport

LOG = logging.getLogger(__name__)


class SessionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StreamStats:
    packets: int
    payload_bytes: int
    wire_bytes: int
    elapsed_s: float

    @property
    def packets_per_second(self) -> float:
        return self.packets / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def megabits_per_second(self) -> float:
        return (self.payload_bytes * 8 / 1_000_000) / self.elapsed_s if self.elapsed_s > 0 else 0.0


class HCCASTSession:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self.codec = FrameCodec()
        self.parser = FrameStreamParser()
        self.device_info: DeviceInfo | None = None
        self._device_ready = threading.Event()
        self._stop_seen = threading.Event()
        self._reader_stop = threading.Event()
        self._reader_error: BaseException | None = None
        self._reader = threading.Thread(target=self._reader_loop, name="hccast-reader", daemon=True)
        self._write_lock = threading.Lock()
        self._started = False

    @property
    def stop_seen(self) -> bool:
        return self._stop_seen.is_set()

    def start_reader(self) -> None:
        if self._started:
            return
        self._started = True
        self._reader.start()

    def _send(self, frame_bytes: bytes) -> None:
        with self._write_lock:
            self.transport.write(frame_bytes)

    def _reader_loop(self) -> None:
        try:
            while not self._reader_stop.is_set():
                data = self.transport.read(timeout_ms=500)
                if not data:
                    continue
                for frame in self.parser.feed(data):
                    self._handle_frame(frame)
        except BaseException as exc:  # Preserve transport failures for the main thread.
            if self._reader_stop.is_set():
                LOG.debug("HCCAST reader stopped during shutdown: %s", exc)
                return
            self._reader_error = exc
            self._device_ready.set()
            self._stop_seen.set()
            LOG.exception("HCCAST reader stopped with an error")

    def _handle_frame(self, frame: Frame) -> None:
        raw_name = frame.raw_command.hex() if frame.raw_command else ""
        LOG.debug(
            "RX seq=%d cmd=%s%s flags=%d payload=%d",
            frame.sequence,
            frame.command.name,
            f"({raw_name})" if raw_name else "",
            frame.flags,
            len(frame.payload),
        )
        if frame.command is Command.SETV:
            try:
                self.device_info = parse_setv(frame)
            except Exception as exc:
                self._reader_error = SessionError(f"invalid SETV response: {exc}")
            finally:
                self._device_ready.set()
        elif frame.command is Command.STOP:
            LOG.warning("display sent STOP")
            self._stop_seen.set()
        elif frame.command is Command.DBG:
            text = frame.payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            LOG.info("display DBG: %s", text)
        elif frame.command in (Command.SETC, Command.SETF):
            LOG.info("settings acknowledgement %s payload=%s", frame.command.name, frame.payload.hex())
        elif frame.command is Command.PING:
            LOG.debug("display PING payload=%s", frame.payload.hex())

    def _raise_reader_error(self) -> None:
        if self._reader_error is not None:
            if isinstance(self._reader_error, SessionError):
                raise self._reader_error
            raise SessionError(f"reader failed: {self._reader_error}") from self._reader_error

    def handshake(self, *, timeout_s: float = 30.0, retry_s: float = 3.0) -> DeviceInfo:
        """Replicate the factory app: send SETR every three seconds until SETV."""

        self.start_reader()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._raise_reader_error()
            LOG.info("TX SETR device-info request")
            self._send(make_setr(self.codec))
            remaining = min(retry_s, max(0.0, deadline - time.monotonic()))
            if self._device_ready.wait(remaining):
                self._raise_reader_error()
                if self.device_info is None:
                    raise SessionError("device-ready event occurred without valid SETV")
                LOG.info(
                    "HCCAST ready: product=%r version=%s mirror_type=%d resolution=%d "
                    "audio=%d vertical=%d auto=%d full=%d",
                    self.device_info.product,
                    self.device_info.version,
                    self.device_info.mirror_type,
                    self.device_info.mirror_resolution,
                    self.device_info.audio_enabled,
                    self.device_info.vertical_mode,
                    self.device_info.vertical_auto_revolve,
                    self.device_info.full_mode,
                )
                return self.device_info
        raise SessionError("timed out waiting for the display's SETV response")

    def send_settings(self, settings: Settings) -> None:
        self._send(make_sets(self.codec, settings))

    def send_screen_info(self, screen: ScreenInfo) -> None:
        LOG.info(
            "TX SINF orientation=%d encoder=%dx%d source=%dx%d",
            screen.orientation,
            screen.encoder_width,
            screen.encoder_height,
            screen.source_short_side,
            screen.source_long_side,
        )
        self._send(make_sinf(self.codec, screen))

    def send_video_packet(self, encoded_h264: bytes) -> None:
        self._raise_reader_error()
        if self.stop_seen:
            raise SessionError("display requested STOP")
        self._send(make_vid(self.codec, encoded_h264))

    def _stream_packets(
        self,
        packets_iter: object,
        *,
        fps: float,
        max_packets: int | None,
    ) -> StreamStats:
        if fps <= 0:
            raise ValueError("fps must be positive")
        frame_period = 1.0 / fps
        started = time.monotonic()
        next_deadline = started
        packets = 0
        payload_bytes = 0
        wire_bytes = 0

        for packet in packets_iter:  # type: ignore[union-attr]
            self.send_video_packet(packet)
            packets += 1
            payload_bytes += len(packet)
            wire_bytes += len(packet) + 16
            if max_packets is not None and packets >= max_packets:
                break

            next_deadline += frame_period
            delay = next_deadline - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_deadline = time.monotonic()

            if packets % max(1, int(fps)) == 0:
                elapsed = time.monotonic() - started
                LOG.info(
                    "streamed %d packets %.2f MiB at %.1f packets/s %.2f Mbit/s",
                    packets,
                    payload_bytes / (1024 * 1024),
                    packets / elapsed,
                    (payload_bytes * 8 / 1_000_000) / elapsed,
                )

        elapsed = max(0.000001, time.monotonic() - started)
        return StreamStats(packets, payload_bytes, wire_bytes, elapsed)

    def stream_h264_file(
        self,
        path: str | Path,
        *,
        fps: float = 30.0,
        packetization: str = "access-unit",
        loop: bool = False,
        max_packets: int | None = None,
    ) -> StreamStats:
        total_packets = 0
        total_payload = 0
        total_wire = 0
        total_elapsed = 0.0
        while True:
            stats = self._stream_packets(
                iter_h264_packets(path, packetization=packetization),
                fps=fps,
                max_packets=(None if max_packets is None else max_packets - total_packets),
            )
            total_packets += stats.packets
            total_payload += stats.payload_bytes
            total_wire += stats.wire_bytes
            total_elapsed += stats.elapsed_s
            if not loop or stats.packets == 0 or (max_packets is not None and total_packets >= max_packets):
                break
        return StreamStats(total_packets, total_payload, total_wire, max(total_elapsed, 0.000001))

    def stream_h264_stream(
        self,
        stream: BinaryIO,
        *,
        fps: float = 30.0,
        packetization: str = "access-unit",
        max_packets: int | None = None,
    ) -> StreamStats:
        return self._stream_packets(
            iter_h264_packets_from_stream(stream, packetization=packetization),
            fps=fps,
            max_packets=max_packets,
        )

    def close(self) -> None:
        self._reader_stop.set()
        if self._started and self._reader.is_alive():
            # Let a normal bounded transport read observe the stop flag before
            # closing its file descriptor underneath the reader thread.
            self._reader.join(timeout=1.0)
        try:
            self.transport.close()
        finally:
            if self._started and self._reader.is_alive():
                self._reader.join(timeout=1.0)
