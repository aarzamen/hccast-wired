from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hccast_wired import cli, setr_probe
from hccast_wired.host_usb import HostUSBTransport
from hccast_wired.protocol import Command, Frame
from hccast_wired.setr_probe import (
    SETR_ONCE_BYTES,
    SetrOnceClassification,
    SetrOnceResult,
    run_setr_once,
)
from hccast_wired.transport import TransportError


EXPECTED_SETR = bytes.fromhex(
    "00 00 00 14 "
    "00 00 00 00 "
    "52 54 45 53 "
    "00 00 00 01 "
    "00 00 00 00"
)


@dataclass
class _Clock:
    now: float = 10.0

    def monotonic(self) -> float:
        return self.now


class _Transport:
    def __init__(
        self,
        reads: list[bytes | Exception] | None = None,
        *,
        write_error: Exception | None = None,
        clock: _Clock | None = None,
        read_advances: list[float] | None = None,
    ) -> None:
        self.reads = list(reads or [])
        self.write_error = write_error
        self.clock = clock
        self.read_advances = list(read_advances or [])
        self.writes: list[bytes] = []
        self.read_timeouts: list[int] = []

    def write(self, data: bytes) -> None:
        raise AssertionError("one-shot probe must bypass looping/ZLP transport.write")

    def write_single_transfer_no_zlp(self, data: bytes) -> None:
        self.writes.append(bytes(data))
        if self.write_error is not None:
            raise self.write_error

    def read(self, *, timeout_ms: int = 500) -> bytes:
        self.read_timeouts.append(timeout_ms)
        if self.clock is not None and self.read_advances:
            self.clock.now += self.read_advances.pop(0)
        if not self.reads:
            return b""
        result = self.reads.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def close(self) -> None:
        raise AssertionError("core probe must not own transport cleanup")


def _valid_setv_bytes() -> bytes:
    payload = bytearray(304)
    payload[12:19] = b"HCCAST\0"
    return Frame(sequence=4, command=Command.SETV, flags=0, payload=bytes(payload)).to_bytes()


def test_exact_one_shot_setr_bytes_are_frozen() -> None:
    assert SETR_ONCE_BYTES == EXPECTED_SETR
    assert len(SETR_ONCE_BYTES) == 20


def test_twenty_byte_setr_causes_one_bulk_write_and_no_zero_length_packet() -> None:
    class _BulkOut:
        # Deliberately equal to SETR length: the ordinary write() method would add a ZLP.
        wMaxPacketSize = 20

        def __init__(self) -> None:
            self.calls: list[tuple[bytes, int]] = []

        def write(self, data: object, *, timeout: int) -> int:
            payload = bytes(data)  # type: ignore[arg-type]
            self.calls.append((payload, timeout))
            return len(payload)

    endpoint = _BulkOut()
    transport = object.__new__(HostUSBTransport)
    transport._ep_out = endpoint
    transport.write_chunk = 16_384
    transport.timeout_ms = 500

    transport.write_single_transfer_no_zlp(SETR_ONCE_BYTES)

    assert endpoint.calls == [(EXPECTED_SETR, 500)]


def test_single_transfer_short_write_fails_without_retry() -> None:
    class _ShortBulkOut:
        wMaxPacketSize = 512

        def __init__(self) -> None:
            self.calls: list[bytes] = []

        def write(self, data: object, *, timeout: int) -> int:
            del timeout
            payload = bytes(data)  # type: ignore[arg-type]
            self.calls.append(payload)
            return len(payload) - 1

    endpoint = _ShortBulkOut()
    transport = object.__new__(HostUSBTransport)
    transport._ep_out = endpoint
    transport.timeout_ms = 500

    with pytest.raises(TransportError, match="short USB bulk OUT.*19 of 20"):
        transport.write_single_transfer_no_zlp(SETR_ONCE_BYTES)

    assert endpoint.calls == [EXPECTED_SETR]


def test_probe_writes_exactly_once_and_persists_before_parse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = b"not-hccast"
    output = tmp_path / "response.bin"
    transport = _Transport([raw, b""])
    classify_calls: list[bytes] = []

    def classify(data: bytes) -> tuple[SetrOnceClassification, object | None, tuple[str, ...]]:
        assert output.read_bytes() == raw
        classify_calls.append(data)
        return SetrOnceClassification.RAW_NON_HCCAST_RESPONSE, None, ()

    monkeypatch.setattr(setr_probe, "_classify_response", classify)

    result = run_setr_once(transport, raw_output=output, response_window_ms=500)

    assert transport.writes == [EXPECTED_SETR]
    assert classify_calls == [raw]
    assert output.read_bytes() == raw
    assert result.classification is SetrOnceClassification.RAW_NON_HCCAST_RESPONSE


def test_valid_setv_is_parsed_only_after_raw_preservation(tmp_path: Path) -> None:
    raw = _valid_setv_bytes()
    output = tmp_path / "valid-setv.bin"

    result = run_setr_once(_Transport([raw, b""]), raw_output=output, response_window_ms=500)

    assert output.read_bytes() == raw
    assert result.classification is SetrOnceClassification.VALID_SETV
    assert result.setv is not None
    assert result.setv.product == "HCCAST"
    assert result.parse_errors == ()


def test_plausible_incomplete_hccast_header_is_partial(tmp_path: Path) -> None:
    raw = bytes.fromhex("00 00 01 40 00 00 00 03 56 54 45 53 00 00 00 00") + b"short"

    result = run_setr_once(
        _Transport([raw, b""]),
        raw_output=tmp_path / "partial.bin",
        response_window_ms=500,
    )

    assert result.classification is SetrOnceClassification.PARTIAL_HCCAST_RESPONSE
    assert result.response_bytes == len(raw)


def test_non_hccast_bytes_are_classified_raw(tmp_path: Path) -> None:
    raw = b"\xde\xad\xbe\xef random device response"

    result = run_setr_once(
        _Transport([raw, b""]),
        raw_output=tmp_path / "raw.bin",
        response_window_ms=500,
    )

    assert result.classification is SetrOnceClassification.RAW_NON_HCCAST_RESPONSE


def test_successful_write_with_no_response_is_inconclusive(tmp_path: Path) -> None:
    output = tmp_path / "empty.bin"
    transport = _Transport([b""])

    result = run_setr_once(transport, raw_output=output, response_window_ms=500)

    assert result.classification is SetrOnceClassification.SETR_WRITE_OK_NO_RESPONSE
    assert result.write_error is None
    assert result.read_error is None
    assert result.response_bytes == 0
    assert output.exists()
    assert output.read_bytes() == b""
    assert transport.writes == [EXPECTED_SETR]


def test_write_failure_is_classified_without_retry_or_read(tmp_path: Path) -> None:
    output = tmp_path / "write-failed.bin"
    transport = _Transport(write_error=TransportError("device disappeared during OUT"))

    result = run_setr_once(transport, raw_output=output, response_window_ms=500)

    assert result.classification is SetrOnceClassification.SETR_WRITE_FAILED
    assert result.write_error == "device disappeared during OUT"
    assert result.read_error is None
    assert transport.writes == [EXPECTED_SETR]
    assert transport.read_timeouts == []
    assert output.read_bytes() == b""


def test_read_error_is_recorded_separately_and_received_bytes_survive(tmp_path: Path) -> None:
    raw = b"evidence-before-disconnect"
    transport = _Transport([raw, TransportError("device disconnected during IN")])
    output = tmp_path / "read-error.bin"

    result = run_setr_once(transport, raw_output=output, response_window_ms=500)

    assert result.classification is SetrOnceClassification.RAW_NON_HCCAST_RESPONSE
    assert result.write_error is None
    assert result.read_error == "device disconnected during IN"
    assert output.read_bytes() == raw


def test_response_reads_never_exceed_the_requested_deadline(tmp_path: Path) -> None:
    clock = _Clock()
    transport = _Transport(
        [b"first", b"second"],
        clock=clock,
        read_advances=[0.300, 0.1995],
    )

    result = run_setr_once(
        transport,
        raw_output=tmp_path / "bounded.bin",
        response_window_ms=500,
        monotonic=clock.monotonic,
    )

    assert result.response_bytes == len(b"firstsecond")
    assert transport.read_timeouts[0] == 500
    assert 1 <= transport.read_timeouts[1] <= 200
    assert len(transport.read_timeouts) == 2
    assert max(transport.read_timeouts) <= 500


def test_response_arriving_after_deadline_is_not_accepted_or_preserved(tmp_path: Path) -> None:
    clock = _Clock()
    transport = _Transport(
        [b"arrived-too-late"],
        clock=clock,
        read_advances=[0.5001],
    )
    output = tmp_path / "post-deadline.bin"

    result = run_setr_once(
        transport,
        raw_output=output,
        response_window_ms=500,
        monotonic=clock.monotonic,
    )

    assert transport.read_timeouts == [500]
    assert result.classification is SetrOnceClassification.SETR_WRITE_OK_NO_RESPONSE
    assert result.response_bytes == 0
    assert output.read_bytes() == b""


@pytest.mark.parametrize("window", [0, -1, 501, 10_000])
def test_invalid_response_window_fails_before_any_write(tmp_path: Path, window: int) -> None:
    transport = _Transport()

    with pytest.raises(ValueError, match="response_window_ms must be between 1 and 500"):
        run_setr_once(
            transport,
            raw_output=tmp_path / "invalid.bin",
            response_window_ms=window,
        )

    assert transport.writes == []


def test_cli_constructs_fail_closed_transport_and_never_creates_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    class _CLITransport:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            captured["transport"] = self
            self.closed = False

        def close(self) -> None:
            self.closed = True

    result = SetrOnceResult(
        classification=SetrOnceClassification.SETR_WRITE_OK_NO_RESPONSE,
        outbound_hex=EXPECTED_SETR.hex(),
        raw_output=str(tmp_path / "response.bin"),
        response_bytes=0,
        write_error=None,
        read_error=None,
        parse_errors=(),
        setv=None,
    )

    def fake_probe(transport: object, **kwargs: Any) -> SetrOnceResult:
        captured["probe_transport"] = transport
        captured.update({f"probe_{key}": value for key, value in kwargs.items()})
        return result

    monkeypatch.setattr(cli, "HostUSBTransport", _CLITransport)
    monkeypatch.setattr(cli, "run_setr_once", fake_probe)
    monkeypatch.setattr(
        cli,
        "HCCASTSession",
        lambda transport: pytest.fail("host-setr-once must not construct HCCASTSession"),
    )
    args = cli.build_parser().parse_args(
        [
            "host-setr-once",
            "--vendor-id",
            "0x1cbe",
            "--product-id",
            "0x0005",
            "--interface",
            "0",
            "--wait-seconds",
            "30",
            "--poll-interval",
            "0.01",
            "--try-claim-with-kernel-driver",
            "--response-timeout-ms",
            "500",
            "--raw-output",
            str(tmp_path / "response.bin"),
        ]
    )

    assert args.func(args) == 0
    assert captured["detach_kernel"] is False
    assert captured["allow_configuration_activation"] is False
    assert captured["try_claim_with_kernel_driver"] is True
    assert captured["interface_number"] == 0
    assert captured["probe_transport"] is captured["transport"]
    assert captured["probe_response_window_ms"] == 500
    assert captured["probe_raw_output"] == Path(tmp_path / "response.bin")
    assert captured["transport"].closed is True
    output = json.loads(capsys.readouterr().out)
    assert output["classification"] == "SETR_WRITE_OK_NO_RESPONSE"
    assert output["outbound_hex"] == EXPECTED_SETR.hex()


def test_cli_closes_transport_when_probe_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CLITransport:
        closed = False

        def close(self) -> None:
            self.closed = True

    transport = _CLITransport()
    monkeypatch.setattr(cli, "HostUSBTransport", lambda **kwargs: transport)
    monkeypatch.setattr(
        cli,
        "run_setr_once",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("raw persistence failed")),
    )
    args = cli.build_parser().parse_args(
        ["host-setr-once", "--raw-output", str(tmp_path / "response.bin")]
    )

    with pytest.raises(RuntimeError, match="raw persistence failed"):
        args.func(args)

    assert transport.closed is True


def test_cli_write_failure_emits_json_and_returns_distinct_nonzero_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _CLITransport:
        closed = False

        def close(self) -> None:
            self.closed = True

    transport = _CLITransport()
    result = SetrOnceResult(
        classification=SetrOnceClassification.SETR_WRITE_FAILED,
        outbound_hex=EXPECTED_SETR.hex(),
        raw_output=str(tmp_path / "response.bin"),
        response_bytes=0,
        write_error="short USB bulk OUT: wrote 19 of 20 bytes",
        read_error=None,
        parse_errors=(),
        setv=None,
    )
    monkeypatch.setattr(cli, "HostUSBTransport", lambda **kwargs: transport)
    monkeypatch.setattr(cli, "run_setr_once", lambda *args, **kwargs: result)
    args = cli.build_parser().parse_args(
        ["host-setr-once", "--raw-output", str(tmp_path / "response.bin")]
    )

    assert args.func(args) == 3
    assert transport.closed is True
    output = json.loads(capsys.readouterr().out)
    assert output["classification"] == "SETR_WRITE_FAILED"
    assert output["write_error"] == "short USB bulk OUT: wrote 19 of 20 bytes"


@pytest.mark.parametrize("value", ["0", "-1", "501", "1.5", "nan", "inf"])
def test_cli_rejects_invalid_response_timeout_before_usb_access(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli,
        "HostUSBTransport",
        lambda **kwargs: pytest.fail("invalid window must fail before USB access"),
    )

    with pytest.raises(SystemExit) as raised:
        cli.main(
            [
                "host-setr-once",
                "--response-timeout-ms",
                value,
                "--raw-output",
                "/tmp/never-written.bin",
            ]
        )

    assert raised.value.code == 2
    assert "response-timeout-ms" in capsys.readouterr().err


def test_cli_defaults_to_maximum_500ms_and_requires_raw_output() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["host-setr-once", "--raw-output", "/tmp/response.bin"])
    assert args.response_timeout_ms == 500

    with pytest.raises(SystemExit):
        parser.parse_args(["host-setr-once"])
