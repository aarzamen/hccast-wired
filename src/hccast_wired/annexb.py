"""Streaming H.264 Annex-B helpers.

The DrongScreen APK forwards MediaCodec output buffers directly.  Those buffers
retain Annex-B start codes, and the APK explicitly inspects byte 4 for ``0x65`` to
identify an IDR NAL.  This module therefore *preserves* every 3- or 4-byte start
code instead of stripping it.

Framing contract
----------------

One ``VID`` packet carries one access unit, that is one coded picture together
with the parameter sets and SEI that precede it, and session pacing counts one
packet per frame period.  ``iter_access_units`` therefore groups NAL units into
complete pictures:

* An access unit delimiter (NAL type 9) always starts a new access unit.  The
  project's sample encoder (``scripts/generate-test-pattern.sh``, x264 with
  ``aud=1``) and the live controller's ``x264enc`` pipeline (``aud=true`` in
  ``live/commands.py``) emit them, which keeps the boundary unambiguous.
* Without delimiters, a new access unit starts at a slice (NAL types 1 to 5)
  whose ``first_mb_in_slice`` is zero, following the detection rule in
  H.264 clause 7.4.1.2.4, and at any SEI, parameter set or other prefix NAL
  that follows a slice.  Additional slices of the same picture, end-of-sequence,
  end-of-stream and filler NAL units stay with the picture they belong to.
* A slice NAL unit too short to carry a slice header is rejected with
  ``AnnexBError`` rather than silently forwarded as its own frame.

The remaining unsupported case is arbitrary slice order (ASO), permitted only
in the Baseline profile and not produced by x264 or the sample encoder; there a
picture's first-transmitted slice may not start at macroblock zero, and such
streams should be encoded with delimiters.  ``packetization="nal"`` bypasses
grouping and sends each NAL unit as its own packet, which is paced per NAL,
not per frame.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import BinaryIO
from pathlib import Path

# Coded slice NAL unit types: non-IDR partitions A/B/C and IDR (7.4.1).
VCL_NAL_TYPES = frozenset({1, 2, 3, 4, 5})
# NAL units that may only precede the first slice of an access unit (7.4.1.2.3):
# SEI, SPS, PPS, AUD, SPS extension, prefix NAL, subset SPS, DPS and reserved 17/18.
ACCESS_UNIT_PREFIX_NAL_TYPES = frozenset({6, 7, 8, 9, 13, 14, 15, 16, 17, 18})
AUD_NAL_TYPE = 9


class AnnexBError(ValueError):
    pass


def _find_start_code(data: bytes | bytearray, start: int = 0) -> tuple[int, int] | None:
    limit = len(data) - 2
    i = max(0, start)
    while i < limit:
        if data[i] == 0 and data[i + 1] == 0:
            if i + 3 < len(data) and data[i + 2] == 0 and data[i + 3] == 1:
                return i, 4
            if data[i + 2] == 1:
                return i, 3
        i += 1
    return None


def start_code_length(nal: bytes) -> int:
    if nal.startswith(b"\x00\x00\x00\x01"):
        return 4
    if nal.startswith(b"\x00\x00\x01"):
        return 3
    raise AnnexBError("NAL unit does not begin with an Annex-B start code")


def nal_unit_type(nal: bytes) -> int:
    offset = start_code_length(nal)
    if len(nal) <= offset:
        raise AnnexBError("empty NAL unit")
    return nal[offset] & 0x1F


def iter_nals(stream: BinaryIO, *, chunk_size: int = 256 * 1024) -> Iterator[bytes]:
    """Yield complete Annex-B NAL units, including their original start codes."""

    if chunk_size < 8:
        raise ValueError("chunk_size must be at least 8")

    buffer = bytearray()
    first_found = False

    while True:
        chunk = stream.read(chunk_size)
        eof = not chunk
        if chunk:
            buffer.extend(chunk)

        if not first_found:
            first = _find_start_code(buffer)
            if first is None:
                if eof:
                    if buffer:
                        raise AnnexBError("no Annex-B start code found")
                    return
                # Keep a tiny suffix in case the start code straddles reads.
                if len(buffer) > 4:
                    del buffer[:-4]
                continue
            if first[0] > 0:
                del buffer[: first[0]]
            first_found = True

        while True:
            current = _find_start_code(buffer, 0)
            if current is None or current[0] != 0:
                raise AnnexBError("internal parser lost NAL synchronization")
            following = _find_start_code(buffer, current[1])
            if following is None:
                break
            nal = bytes(buffer[: following[0]])
            del buffer[: following[0]]
            if len(nal) > current[1]:
                yield nal

        if eof:
            if buffer:
                current = _find_start_code(buffer, 0)
                if current is not None and current[0] == 0 and len(buffer) > current[1]:
                    yield bytes(buffer)
            return


def first_mb_in_slice_is_zero(nal: bytes) -> bool:
    """Report whether a slice NAL unit starts at macroblock zero.

    ``first_mb_in_slice`` is the first syntax element of the slice header and is
    coded as ``ue(v)``; the value zero is the single bit ``1``.  The byte that
    carries it directly follows the one-byte NAL header and can never be an
    emulation-prevention byte, so no RBSP unescaping is needed here.
    """

    offset = start_code_length(nal)
    if len(nal) < offset + 2:
        raise AnnexBError(
            "slice NAL unit is too short to carry a slice header; the stream is not "
            "a usable Annex-B H.264 elementary stream"
        )
    return bool(nal[offset + 1] & 0x80)


def iter_access_units(nals: Iterator[bytes]) -> Iterator[bytes]:
    """Group NAL units into complete access units (one coded picture each).

    Boundaries are access unit delimiters when present and, without them, the
    first slice of each picture plus any prefix NAL units that precede it.  See
    the module docstring for the full contract.
    """

    pending: list[bytes] = []
    have_slice = False

    for nal in nals:
        nal_type = nal_unit_type(nal)
        if nal_type == AUD_NAL_TYPE:
            boundary = bool(pending)
        elif nal_type in VCL_NAL_TYPES:
            boundary = have_slice and first_mb_in_slice_is_zero(nal)
        elif nal_type in ACCESS_UNIT_PREFIX_NAL_TYPES:
            boundary = have_slice
        else:
            # End of sequence, end of stream, filler and auxiliary data close
            # or belong to the current picture.
            boundary = False
        if boundary:
            yield b"".join(pending)
            pending = []
            have_slice = False
        pending.append(nal)
        if nal_type in VCL_NAL_TYPES:
            have_slice = True

    if pending:
        yield b"".join(pending)


def iter_h264_packets_from_stream(
    stream: BinaryIO,
    *,
    packetization: str = "access-unit",
    chunk_size: int = 256 * 1024,
) -> Iterator[bytes]:
    nals = iter_nals(stream, chunk_size=chunk_size)
    if packetization == "nal":
        yield from nals
    elif packetization == "access-unit":
        yield from iter_access_units(nals)
    else:
        raise ValueError("packetization must be 'nal' or 'access-unit'")


def iter_h264_packets(
    path: str | Path,
    *,
    packetization: str = "access-unit",
    chunk_size: int = 256 * 1024,
) -> Iterator[bytes]:
    path = Path(path)
    with path.open("rb") as stream:
        yield from iter_h264_packets_from_stream(
            stream, packetization=packetization, chunk_size=chunk_size
        )
