from __future__ import annotations

import io

from hccast_wired.annexb import iter_access_units, iter_nals, nal_unit_type


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
