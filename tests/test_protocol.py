from __future__ import annotations

import struct

import pytest

from hccast_wired.protocol import (
    Command,
    Frame,
    FrameCodec,
    FrameStreamParser,
    ProtocolError,
    ScreenInfo,
    Settings,
    make_setr,
    parse_setv,
)


def test_command_magic_matches_apk() -> None:
    assert Command.VID.value == bytes([0, 68, 73, 86])
    assert Command.AUD.value == bytes([0, 68, 85, 65])
    assert Command.SETR.value == b"RTES"
    assert Command.SETV.value == b"VTES"
    assert Command.SINF.value == b"FNIS"


def test_frame_encoding_and_sequence() -> None:
    codec = FrameCodec()
    first = codec.encode(Command.SETR, b"\0\0\0\0", flags=1)
    second = codec.encode(Command.VID, b"abc")
    assert first[:16] == struct.pack(">II4sI", 20, 0, b"RTES", 1)
    assert second[:16] == struct.pack(">II4sI", 19, 1, b"\0DIV", 0)


def test_make_setr_exact() -> None:
    assert make_setr(FrameCodec()) == struct.pack(">II4sI4s", 20, 0, b"RTES", 1, b"\0\0\0\0")


def test_stream_parser_fragmented_and_coalesced() -> None:
    codec = FrameCodec()
    a = codec.encode(Command.SETC, b"one")
    b = codec.encode(Command.SETF, b"two")
    parser = FrameStreamParser()
    assert parser.feed(a[:7]) == []
    frames = parser.feed(a[7:] + b)
    assert [frame.command for frame in frames] == [Command.SETC, Command.SETF]
    assert [frame.payload for frame in frames] == [b"one", b"two"]


def test_stream_parser_resynchronizes() -> None:
    codec = FrameCodec()
    good = codec.encode(Command.PING, b"x")
    parser = FrameStreamParser()
    frames = parser.feed(b"junk" + good)
    assert len(frames) == 1
    assert frames[0].command is Command.PING
    assert parser.discarded_bytes == 4


def test_settings_and_screen_info_layout() -> None:
    assert Settings(1, 1, 0, 1).to_payload() == struct.pack(">IIII", 1, 1, 0, 1)
    assert ScreenInfo(1, 1280, 720, 720, 1280).to_payload() == struct.pack(
        ">IIIII", 1, 1280, 720, 720, 1280
    )


def test_parse_setv_factory_layout() -> None:
    payload = bytearray(316)
    struct.pack_into(">III", payload, 0, 0, 1, 1)
    payload[12:12 + len(b"X-40F\0")] = b"X-40F\0"
    struct.pack_into(">I", payload, 44, 0x01020304)
    payload[48:48 + len(b"http://192.168.203.1\0")] = b"http://192.168.203.1\0"
    struct.pack_into(">III", payload, 304, 1, 0, 1)
    frame = Frame(7, Command.SETV, 0, bytes(payload))
    info = parse_setv(frame)
    assert info.product == "X-40F"
    assert info.version_raw == 0x01020304
    assert info.url == "http://192.168.203.1"
    assert info.vertical_mode == 1
    assert info.vertical_auto_revolve == 0
    assert info.full_mode == 1


def test_parse_setv_rejects_short_packet() -> None:
    with pytest.raises(ProtocolError):
        parse_setv(Frame(0, Command.SETV, 0, bytes(100)))


def test_large_video_frame_is_not_capped_at_64k() -> None:
    payload = b"\x00\x00\x00\x01\x65" + bytes(100_000)
    frame = FrameCodec().encode(Command.VID, payload)
    assert len(frame) == 16 + len(payload)
    parsed = FrameStreamParser().feed(frame)
    assert len(parsed) == 1
    assert parsed[0].payload == payload
