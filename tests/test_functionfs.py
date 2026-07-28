from __future__ import annotations

import struct

from hccast_wired.functionfs import (
    FUNCTIONFS_ALL_CTRL_RECIP,
    FUNCTIONFS_CONFIG0_SETUP,
    FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
    FUNCTIONFS_HAS_FS_DESC,
    FUNCTIONFS_HAS_HS_DESC,
    FUNCTIONFS_STRINGS_MAGIC,
    build_descriptors,
    build_strings,
)


def test_descriptor_blob_lengths_and_flags() -> None:
    blob = build_descriptors(receive_all_control=False)
    magic, length, flags, fs_count, hs_count = struct.unpack_from("<IIIII", blob, 0)
    assert magic == FUNCTIONFS_DESCRIPTORS_MAGIC_V2
    assert length == len(blob)
    assert flags == FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    assert fs_count == 3
    assert hs_count == 3
    # 20-byte header + 2 * (9-byte interface + 2 * 7-byte endpoints)
    assert len(blob) == 20 + 2 * 23


def test_negotiation_flags() -> None:
    blob = build_descriptors(receive_all_control=True)
    flags = struct.unpack_from("<I", blob, 8)[0]
    assert flags & FUNCTIONFS_ALL_CTRL_RECIP
    assert flags & FUNCTIONFS_CONFIG0_SETUP


def test_string_blob() -> None:
    blob = build_strings("Accessory")
    magic, length, str_count, lang_count = struct.unpack_from("<IIII", blob, 0)
    assert magic == FUNCTIONFS_STRINGS_MAGIC
    assert length == len(blob)
    assert str_count == 1
    assert lang_count == 1
    assert struct.unpack_from("<H", blob, 16)[0] == 0x0409
    assert blob[18:] == b"Accessory\0"
