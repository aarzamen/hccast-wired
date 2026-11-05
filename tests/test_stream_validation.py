"""Stream input and limit validation happens before any USB write.

Every rejection below is checked against a transport that records writes, and
the command-line checks replace the USB transport and gadget with objects that
fail the test if they are ever constructed or set up.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest

from hccast_wired import cli
from hccast_wired.session import HCCASTSession, validate_stream_file, validate_stream_limits
from hccast_wired.transport import Transport

AUD = b"\x00\x00\x00\x01\x09\xf0"
IDR = b"\x00\x00\x00\x01\x65abc"
P_SLICE = b"\x00\x00\x00\x01\x41def"
TWO_FRAMES = AUD + IDR + AUD + P_SLICE


class RecordingTransport(Transport):
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    def read(self, *, timeout_ms: int = 500) -> bytes:
        del timeout_ms
        return b""

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def sample(tmp_path: Path) -> Path:
    path = tmp_path / "two-frames.h264"
    path.write_bytes(TWO_FRAMES)
    return path


@pytest.mark.parametrize("fps", [0, -1, float("nan"), float("inf"), float("-inf"), True, "30"])
def test_api_rejects_non_positive_or_non_finite_fps_without_writing(sample: Path, fps: Any) -> None:
    transport = RecordingTransport()
    session = HCCASTSession(transport)

    with pytest.raises(ValueError, match="fps"):
        session.stream_h264_file(sample, fps=fps)
    with pytest.raises(ValueError, match="fps"):
        session.stream_h264_stream(io.BytesIO(TWO_FRAMES), fps=fps)

    assert transport.writes == []


@pytest.mark.parametrize("limit", [0, -1, -100, True, 1.5])
def test_api_rejects_limits_that_cannot_send_a_packet_without_writing(sample: Path, limit: Any) -> None:
    transport = RecordingTransport()
    session = HCCASTSession(transport)

    with pytest.raises(ValueError, match="max_packets"):
        session.stream_h264_file(sample, fps=1000, max_packets=limit)
    with pytest.raises(ValueError, match="max_packets"):
        session.stream_h264_stream(io.BytesIO(TWO_FRAMES), fps=1000, max_packets=limit)

    assert transport.writes == []


def test_api_rejects_missing_or_unreadable_file_before_writing(tmp_path: Path) -> None:
    transport = RecordingTransport()
    session = HCCASTSession(transport)

    with pytest.raises(ValueError, match="existing regular file"):
        session.stream_h264_file(tmp_path / "absent.h264", fps=1000)
    with pytest.raises(ValueError, match="existing regular file"):
        session.stream_h264_file(tmp_path, fps=1000)

    assert transport.writes == []


def test_limit_sends_exactly_that_many_packets(sample: Path) -> None:
    for limit, expected in ((1, 1), (2, 2), (5, 2), (None, 2)):
        transport = RecordingTransport()
        session = HCCASTSession(transport)

        stats = session.stream_h264_file(sample, fps=1000, max_packets=limit)

        assert (stats.packets, len(transport.writes)) == (expected, expected), limit
    transport = RecordingTransport()
    stats = HCCASTSession(transport).stream_h264_file(sample, fps=1000, loop=True, max_packets=3)
    assert (stats.packets, len(transport.writes)) == (3, 3)


def test_validation_helpers_accept_sound_input(sample: Path) -> None:
    validate_stream_limits(30.0, None)
    validate_stream_limits(1, 1)
    assert validate_stream_file(sample) == sample
    assert validate_stream_file(str(sample)) == sample


@pytest.fixture
def no_usb(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_transport(**_kwargs: Any) -> None:
        raise AssertionError("the host transport must not be opened for rejected input")

    def forbidden_create(self: Any, **_kwargs: Any) -> None:
        raise AssertionError("the gadget must not be created for rejected input")

    def forbidden_control(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("FunctionFS must not be opened for rejected input")

    monkeypatch.setattr(cli, "HostUSBTransport", forbidden_transport)
    monkeypatch.setattr(cli.ConfigFSGadget, "create", forbidden_create)
    monkeypatch.setattr(cli.ConfigFSGadget, "ensure_configfs", lambda self: None)
    monkeypatch.setattr(cli, "FunctionFSControl", forbidden_control)


@pytest.mark.parametrize("command", ["host-stream", "gadget-stream"])
@pytest.mark.parametrize(
    "extra",
    [
        ["--max-packets", "0"],
        ["--max-packets", "-3"],
        ["--max-packets", "1.5"],
        ["--fps", "0"],
        ["--fps", "-30"],
        ["--fps", "nan"],
        ["--fps", "inf"],
        ["--fps", "fast"],
    ],
)
def test_cli_rejects_impossible_limits_at_parse_time(
    no_usb: None, sample: Path, command: str, extra: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main([command, str(sample), *extra])

    assert raised.value.code == 2
    assert extra[0] in capsys.readouterr().err


@pytest.mark.parametrize("command", ["host-stream", "gadget-stream"])
def test_cli_rejects_missing_file_and_stdin_loop_before_usb_setup(
    no_usb: None, tmp_path: Path, command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main([command, str(tmp_path / "absent.h264")]) == 1
    assert "existing regular file" in capsys.readouterr().err

    assert cli.main([command, str(tmp_path)]) == 1
    assert "existing regular file" in capsys.readouterr().err

    assert cli.main([command, "-", "--loop"]) == 1
    assert "--loop cannot be used with stdin" in capsys.readouterr().err


def test_cli_accepts_valid_stream_arguments(sample: Path) -> None:
    args = cli.build_parser().parse_args(
        ["host-stream", str(sample), "--fps", "10", "--max-packets", "300"]
    )
    cli.validate_stream_arguments(args)
    assert (args.fps, args.max_packets) == (10.0, 300)

    stdin_args = cli.build_parser().parse_args(["gadget-stream", "-"])
    cli.validate_stream_arguments(stdin_args)
