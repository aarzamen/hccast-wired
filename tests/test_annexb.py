from __future__ import annotations

import io

import pytest

from hccast_wired.annexb import (
    AnnexBError,
    first_mb_in_slice_is_zero,
    iter_access_units,
    iter_h264_packets_from_stream,
    iter_nals,
    nal_unit_type,
)
from hccast_wired.session import HCCASTSession
from hccast_wired.transport import Transport

START = b"\x00\x00\x00\x01"
AUD = START + b"\x09\xf0"
SPS = START + b"\x67\x42\xc0\x1f"
PPS = START + b"\x68\xce\x3c\x80"
SEI = START + b"\x06\x05\x01\x80"
EOS = START + b"\x0a"
# Slice data bytes: first_mb_in_slice is ue(v); 0 is the single bit "1" (0x80),
# 5 is "00110" (0x30), so a second slice of the same picture starts with 0x30.
IDR_SLICE_0 = START + b"\x65\x88\x84\x00"
IDR_SLICE_5 = START + b"\x65\x30\x84\x00"
P_SLICE_0 = START + b"\x41\x9a\x02\x00"
P_SLICE_5 = START + b"\x41\x30\x02\x00"


class RecordingTransport(Transport):
    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, *, timeout_ms: int = 500) -> bytes:
        del timeout_ms
        return b""

    def close(self) -> None:
        pass


def test_iter_nals_preserves_mixed_start_codes_across_chunks() -> None:
    stream = (
        b"garbage"
        b"\x00\x00\x00\x01\x67abc"
        b"\x00\x00\x01\x68def"
        b"\x00\x00\x00\x01\x65ghi"
    )
    nals = list(iter_nals(io.BytesIO(stream), chunk_size=9))
    assert nals == [
        b"\x00\x00\x00\x01\x67abc",
        b"\x00\x00\x01\x68def",
        b"\x00\x00\x00\x01\x65ghi",
    ]
    assert [nal_unit_type(nal) for nal in nals] == [7, 8, 5]


def test_access_units_group_on_aud() -> None:
    nals = iter(
        [
            b"\x00\x00\x00\x01\x09\xf0",
            b"\x00\x00\x00\x01\x67a",
            b"\x00\x00\x00\x01\x65b",
            b"\x00\x00\x00\x01\x09\xf0",
            b"\x00\x00\x00\x01\x41c",
        ]
    )
    units = list(iter_access_units(nals))
    assert len(units) == 2
    assert units[0].startswith(b"\x00\x00\x00\x01\x09")
    assert units[0].endswith(b"\x65b")
    assert units[1].endswith(b"\x41c")


def test_first_mb_in_slice_detection_reads_the_first_slice_header_bit() -> None:
    assert first_mb_in_slice_is_zero(IDR_SLICE_0) is True
    assert first_mb_in_slice_is_zero(P_SLICE_0) is True
    assert first_mb_in_slice_is_zero(IDR_SLICE_5) is False
    assert first_mb_in_slice_is_zero(b"\x00\x00\x01\x41\x80") is True


def test_access_units_without_delimiters_group_each_picture_with_its_prefix() -> None:
    units = list(iter_access_units(iter([SPS, PPS, IDR_SLICE_0, P_SLICE_0, P_SLICE_0])))

    assert units == [SPS + PPS + IDR_SLICE_0, P_SLICE_0, P_SLICE_0]


def test_multiple_slices_of_one_picture_stay_in_one_access_unit() -> None:
    units = list(iter_access_units(iter([IDR_SLICE_0, IDR_SLICE_5, P_SLICE_0, P_SLICE_5])))

    assert units == [IDR_SLICE_0 + IDR_SLICE_5, P_SLICE_0 + P_SLICE_5]


def test_sei_and_parameter_sets_after_a_slice_start_the_next_access_unit() -> None:
    units = list(iter_access_units(iter([IDR_SLICE_0, SEI, P_SLICE_0, SPS, PPS, IDR_SLICE_0])))

    assert units == [IDR_SLICE_0, SEI + P_SLICE_0, SPS + PPS + IDR_SLICE_0]


def test_end_of_sequence_stays_with_the_picture_it_closes() -> None:
    units = list(iter_access_units(iter([IDR_SLICE_0, P_SLICE_0, EOS, IDR_SLICE_0])))

    assert units == [IDR_SLICE_0, P_SLICE_0 + EOS, IDR_SLICE_0]


def test_delimited_stream_keeps_leading_nal_units_in_the_first_access_unit() -> None:
    units = list(iter_access_units(iter([SPS, PPS, AUD, IDR_SLICE_0, AUD, P_SLICE_0])))

    assert units == [SPS + PPS, AUD + IDR_SLICE_0, AUD + P_SLICE_0]


def test_truncated_slice_is_rejected_with_a_useful_message() -> None:
    with pytest.raises(AnnexBError, match="too short to carry a slice header"):
        list(iter_access_units(iter([IDR_SLICE_0, START + b"\x41"])))


def test_stream_without_delimiters_is_paced_per_picture_not_per_nal() -> None:
    stream = SPS + PPS + IDR_SLICE_0 + IDR_SLICE_5 + P_SLICE_0 + P_SLICE_5 + P_SLICE_0
    frames = list(iter_h264_packets_from_stream(io.BytesIO(stream), packetization="access-unit"))
    nal_units = list(iter_h264_packets_from_stream(io.BytesIO(stream), packetization="nal"))
    assert len(frames) == 3
    assert len(nal_units) == 7

    transport = RecordingTransport()
    stats = HCCASTSession(transport).stream_h264_stream(io.BytesIO(stream), fps=1000)

    assert stats.packets == 3
    assert len(transport.writes) == 3
    assert transport.writes[0].endswith(SPS + PPS + IDR_SLICE_0 + IDR_SLICE_5)
