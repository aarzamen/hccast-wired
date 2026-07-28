"""HCCAST USB mirror protocol framing recovered from DrongScreen 3.2.11.

The on-wire header is 16 bytes:

    0x00  uint32 BE total frame length, including the header
    0x04  uint32 BE sequence counter
    0x08  4-byte command magic
    0x0c  uint32 BE command flags/argument
    0x10  payload

The command bytes look reversed when read as ASCII because the APK stores them as
literal byte arrays.  For example, video is ``00 44 49 56`` (``\0DIV``), but the
factory code names the command ``VID``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Final

HEADER_SIZE: Final[int] = 16
DEFAULT_MAX_FRAME: Final[int] = 32 * 1024 * 1024
_HEADER = struct.Struct(">II4sI")
_U32 = struct.Struct(">I")


class ProtocolError(ValueError):
    """Raised when a malformed HCCAST frame is encountered."""


class Command(Enum):
    UNK = b"\x00\x00\x00\x00"
    VID = b"\x00DIV"
    AUD = b"\x00DUA"
    SETS = b"STES"
    SETR = b"RTES"
    SETC = b"CTES"
    SETF = b"FTES"
    SETV = b"VTES"
    DBG = b"\x00GBD"
    STOP = b"POTS"
    UPG = b"\x00GPU"
    UPGI = b"IGPU"
    PING = b"GNIP"
    SINF = b"FNIS"

    @classmethod
    def from_magic(cls, magic: bytes) -> "Command":
        for command in cls:
            if command.value == magic:
                return command
        return cls.UNK


@dataclass(frozen=True, slots=True)
class Frame:
    sequence: int
    command: Command
    flags: int
    payload: bytes
    raw_command: bytes | None = None

    @property
    def total_length(self) -> int:
        return HEADER_SIZE + len(self.payload)

    def to_bytes(self) -> bytes:
        magic = self.command.value if self.command is not Command.UNK else self.raw_command
        if magic is None or len(magic) != 4:
            raise ProtocolError("unknown commands require a four-byte raw_command")
        return _HEADER.pack(
            self.total_length,
            self.sequence & 0xFFFFFFFF,
            magic,
            self.flags & 0xFFFFFFFF,
        ) + self.payload


class FrameCodec:
    """Stateful HCCAST frame encoder with the APK's global sequence semantics."""

    def __init__(self, sequence: int = 0) -> None:
        self._sequence = sequence & 0xFFFFFFFF

    @property
    def next_sequence(self) -> int:
        return self._sequence

    def build(
        self,
        command: Command,
        payload: bytes = b"",
        *,
        flags: int | None = None,
    ) -> Frame:
        if flags is None:
            flags = 1 if command is Command.SETR else 0
        frame = Frame(self._sequence, command, flags, bytes(payload))
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        return frame

    def encode(
        self,
        command: Command,
        payload: bytes = b"",
        *,
        flags: int | None = None,
    ) -> bytes:
        return self.build(command, payload, flags=flags).to_bytes()


class FrameStreamParser:
    """Incremental parser for an arbitrary USB byte stream.

    USB transfer boundaries are not HCCAST message boundaries.  This parser accepts
    fragmented and coalesced messages.  On obvious corruption, it scans forward for
    a plausible length + known command header rather than permanently wedging.
    """

    def __init__(self, *, max_frame: int = DEFAULT_MAX_FRAME) -> None:
        if max_frame < HEADER_SIZE:
            raise ValueError("max_frame must be at least 16")
        self.max_frame = max_frame
        self._buffer = bytearray()
        self.discarded_bytes = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        if data:
            self._buffer.extend(data)
        frames: list[Frame] = []

        while len(self._buffer) >= HEADER_SIZE:
            total_length = _U32.unpack_from(self._buffer, 0)[0]
            if not self._plausible_length(total_length):
                self._resync()
                continue
            if len(self._buffer) < total_length:
                break

            total, sequence, magic, flags = _HEADER.unpack_from(self._buffer, 0)
            payload = bytes(self._buffer[HEADER_SIZE:total])
            del self._buffer[:total]
            command = Command.from_magic(magic)
            frames.append(
                Frame(
                    sequence=sequence,
                    command=command,
                    flags=flags,
                    payload=payload,
                    raw_command=magic if command is Command.UNK else None,
                )
            )

        return frames

    def _plausible_length(self, value: int) -> bool:
        return HEADER_SIZE <= value <= self.max_frame

    def _resync(self) -> None:
        # Look for a known command at header offset +8 and a sane length.  Keep the
        # final 15 bytes if no candidate exists because they may be a partial header.
        known = {command.value for command in Command if command is not Command.UNK}
        search_limit = max(1, len(self._buffer) - HEADER_SIZE + 1)
        for offset in range(1, search_limit):
            if len(self._buffer) - offset < HEADER_SIZE:
                break
            length = _U32.unpack_from(self._buffer, offset)[0]
            magic = bytes(self._buffer[offset + 8 : offset + 12])
            if self._plausible_length(length) and magic in known:
                del self._buffer[:offset]
                self.discarded_bytes += offset
                return

        discard = max(1, len(self._buffer) - (HEADER_SIZE - 1))
        del self._buffer[:discard]
        self.discarded_bytes += discard


@dataclass(frozen=True, slots=True)
class Settings:
    mirror_resolution: int = 1
    vertical_mode: int = 1
    vertical_auto_revolve: int = 0
    full_mode: int = 1

    def to_payload(self) -> bytes:
        values = (
            self.mirror_resolution,
            self.vertical_mode,
            self.vertical_auto_revolve,
            self.full_mode,
        )
        if not (0 <= self.mirror_resolution <= 3):
            raise ValueError("mirror_resolution must be 0..3")
        if not (0 <= self.vertical_mode <= 3):
            raise ValueError("vertical_mode must be 0..3")
        if self.vertical_auto_revolve not in (0, 1):
            raise ValueError("vertical_auto_revolve must be 0 or 1")
        if self.full_mode not in (0, 1):
            raise ValueError("full_mode must be 0 or 1")
        return struct.pack(">IIII", *values)


@dataclass(frozen=True, slots=True)
class ScreenInfo:
    orientation: int
    encoder_width: int
    encoder_height: int
    source_short_side: int
    source_long_side: int

    def to_payload(self) -> bytes:
        if self.orientation not in (0, 1):
            raise ValueError("orientation must be 0 (portrait) or 1 (landscape)")
        values = (
            self.orientation,
            self.encoder_width,
            self.encoder_height,
            self.source_short_side,
            self.source_long_side,
        )
        if any(value < 0 for value in values):
            raise ValueError("screen info values must be non-negative")
        return struct.pack(">IIIII", *values)


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    mirror_type: int
    mirror_resolution: int
    audio_enabled: int
    product: str
    version_raw: int
    version: str
    url: str
    vertical_mode: int = 0
    vertical_auto_revolve: int = 0
    full_mode: int = 0


def _be_u32(payload: bytes, offset: int, default: int = 0) -> int:
    if len(payload) < offset + 4:
        return default
    return _U32.unpack_from(payload, offset)[0]


def _c_string(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("utf-8", errors="replace")


def parse_setv(frame: Frame) -> DeviceInfo:
    """Parse the factory app's SETV device-info response layout.

    The APK accepts a minimum *total packet* length of 320 bytes.  That means a
    304-byte base payload, with optional 4-byte orientation fields after it.
    """

    if frame.command is not Command.SETV:
        raise ProtocolError(f"expected SETV, got {frame.command.name}")
    if frame.total_length < 320:
        raise ProtocolError(
            f"SETV is too short: total={frame.total_length}, factory app requires >=320"
        )

    payload = frame.payload
    mirror_type = _be_u32(payload, 0)
    mirror_resolution = _be_u32(payload, 4)
    audio_enabled = _be_u32(payload, 8)
    product = _c_string(payload[12:44])

    # The APK reverses these four bytes, then reads a little-endian int.  The
    # resulting numeric value is equivalent to interpreting the original bytes BE.
    version_raw = _be_u32(payload, 44)
    version = str(version_raw & 0xFFFFFFFF)
    url = _c_string(payload[48:304]).strip()

    return DeviceInfo(
        mirror_type=mirror_type,
        mirror_resolution=mirror_resolution,
        audio_enabled=audio_enabled,
        product=product,
        version_raw=version_raw,
        version=version,
        url=url,
        vertical_mode=_be_u32(payload, 304),
        vertical_auto_revolve=_be_u32(payload, 308),
        full_mode=_be_u32(payload, 312),
    )


def make_setr(codec: FrameCodec) -> bytes:
    return codec.encode(Command.SETR, b"\x00\x00\x00\x00", flags=1)


def make_sets(codec: FrameCodec, settings: Settings) -> bytes:
    return codec.encode(Command.SETS, settings.to_payload())


def make_sinf(codec: FrameCodec, info: ScreenInfo) -> bytes:
    return codec.encode(Command.SINF, info.to_payload())


def make_vid(codec: FrameCodec, encoded_h264: bytes) -> bytes:
    if not encoded_h264:
        raise ValueError("video payload must not be empty")
    return codec.encode(Command.VID, encoded_h264)


def make_aud(codec: FrameCodec, pcm: bytes) -> bytes:
    if not pcm:
        raise ValueError("audio payload must not be empty")
    return codec.encode(Command.AUD, pcm)
