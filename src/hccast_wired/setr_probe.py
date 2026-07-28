"""Safety-bounded one-shot HCCAST SETR identity probe."""

from __future__ import annotations

import struct
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Final, Protocol

from .protocol import (
    DEFAULT_MAX_FRAME,
    HEADER_SIZE,
    Command,
    DeviceInfo,
    FrameStreamParser,
    parse_setv,
)

SETR_ONCE_BYTES: Final[bytes] = bytes.fromhex(
    "00 00 00 14 00 00 00 00 52 54 45 53 00 00 00 01 00 00 00 00"
)
MIN_RESPONSE_WINDOW_MS: Final[int] = 1
MAX_RESPONSE_WINDOW_MS: Final[int] = 500
_U32 = struct.Struct(">I")


class SetrOnceClassification(str, Enum):
    VALID_SETV = "VALID_SETV"
    PARTIAL_HCCAST_RESPONSE = "PARTIAL_HCCAST_RESPONSE"
    RAW_NON_HCCAST_RESPONSE = "RAW_NON_HCCAST_RESPONSE"
    SETR_WRITE_OK_NO_RESPONSE = "SETR_WRITE_OK_NO_RESPONSE"
    SETR_WRITE_FAILED = "SETR_WRITE_FAILED"


@dataclass(frozen=True, slots=True)
class SetrOnceResult:
    classification: SetrOnceClassification
    outbound_hex: str
    raw_output: str
    response_bytes: int
    write_error: str | None
    read_error: str | None
    parse_errors: tuple[str, ...]
    setv: DeviceInfo | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification.value,
            "outbound_hex": self.outbound_hex,
            "raw_output": self.raw_output,
            "response_bytes": self.response_bytes,
            "write_error": self.write_error,
            "read_error": self.read_error,
            "parse_errors": list(self.parse_errors),
            "setv": asdict(self.setv) if self.setv is not None else None,
        }


class SetrOnceTransport(Protocol):
    def write_single_transfer_no_zlp(self, data: bytes) -> None: ...

    def read(self, *, timeout_ms: int = 500) -> bytes: ...


def _validate_response_window_ms(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("response_window_ms must be an integer between 1 and 500")
    if not MIN_RESPONSE_WINDOW_MS <= value <= MAX_RESPONSE_WINDOW_MS:
        raise ValueError("response_window_ms must be between 1 and 500")


def _plausible_incomplete_hccast_frame(data: bytes) -> bool:
    """Recognize only an incomplete header-bearing frame, never arbitrary short bytes."""

    known_magic = {command.value for command in Command if command is not Command.UNK}
    # Twelve bytes are required to establish both a declared length and known command.
    for offset in range(max(0, len(data) - 11)):
        remaining = len(data) - offset
        if remaining < 12:
            break
        total_length = _U32.unpack_from(data, offset)[0]
        magic = data[offset + 8 : offset + 12]
        if (
            HEADER_SIZE <= total_length <= DEFAULT_MAX_FRAME
            and magic in known_magic
            and remaining < total_length
        ):
            return True
    return False


def _classify_response(
    data: bytes,
) -> tuple[SetrOnceClassification, DeviceInfo | None, tuple[str, ...]]:
    parser = FrameStreamParser()
    parse_errors: list[str] = []
    valid_setv: DeviceInfo | None = None

    for frame in parser.feed(data):
        if frame.command is not Command.SETV:
            continue
        try:
            valid_setv = parse_setv(frame)
        except Exception as exc:
            parse_errors.append(f"invalid SETV: {exc}")
            continue
        break

    if valid_setv is not None:
        return SetrOnceClassification.VALID_SETV, valid_setv, tuple(parse_errors)
    if _plausible_incomplete_hccast_frame(data):
        return SetrOnceClassification.PARTIAL_HCCAST_RESPONSE, None, tuple(parse_errors)
    return SetrOnceClassification.RAW_NON_HCCAST_RESPONSE, None, tuple(parse_errors)


def run_setr_once(
    transport: SetrOnceTransport,
    *,
    raw_output: str | Path,
    response_window_ms: int,
    monotonic: Callable[[], float] | None = None,
) -> SetrOnceResult:
    """Write one SETR, preserve all response bytes, then classify the preserved bytes.

    Transport lifecycle ownership remains with the caller so the CLI can release the
    claimed interface in one ``finally`` block regardless of how this probe ends.
    """

    _validate_response_window_ms(response_window_ms)
    clock = time.monotonic if monotonic is None else monotonic
    output_path = Path(raw_output).expanduser()
    collected = bytearray()
    write_error: str | None = None
    read_error: str | None = None

    try:
        # This is deliberately the only application-level OUT operation in the probe.
        transport.write_single_transfer_no_zlp(SETR_ONCE_BYTES)
    except Exception as exc:
        write_error = str(exc)

    if write_error is None:
        deadline = clock() + response_window_ms / 1000.0
        while True:
            remaining_s = deadline - clock()
            # Floor to whole milliseconds. A ceil could ask libusb to block past the
            # authorized window; a sub-millisecond remainder is intentionally unused.
            remaining_ms = int(remaining_s * 1000.0)
            if remaining_ms < MIN_RESPONSE_WINDOW_MS:
                break
            timeout_ms = min(response_window_ms, remaining_ms)
            try:
                chunk = transport.read(timeout_ms=timeout_ms)
            except Exception as exc:
                read_error = str(exc)
                break
            # A transport can return after its deadline. Those bytes are outside the
            # authorized collection window and must not enter the evidence buffer.
            if clock() > deadline:
                break
            if not chunk:
                # HostUSBTransport returns an empty read only after the requested
                # libusb timeout, so the bounded collection window is complete.
                break
            collected.extend(chunk)

    raw = bytes(collected)
    # Raw preservation is the trust boundary: parsing is forbidden before this write.
    output_path.write_bytes(raw)

    if write_error is not None:
        classification = SetrOnceClassification.SETR_WRITE_FAILED
        setv = None
        parse_errors: tuple[str, ...] = ()
    elif not raw:
        classification = SetrOnceClassification.SETR_WRITE_OK_NO_RESPONSE
        setv = None
        parse_errors = ()
    else:
        classification, setv, parse_errors = _classify_response(raw)

    return SetrOnceResult(
        classification=classification,
        outbound_hex=SETR_ONCE_BYTES.hex(),
        raw_output=str(output_path),
        response_bytes=len(raw),
        write_error=write_error,
        read_error=read_error,
        parse_errors=parse_errors,
        setv=setv,
    )
