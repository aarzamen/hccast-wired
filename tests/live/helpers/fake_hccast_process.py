#!/usr/bin/env python3
"""Small local-only stand-in for the HCCAST gadget process.

This helper deliberately uses ordinary buffered ``print`` calls for its protocol
output.  Integration tests must set ``PYTHONUNBUFFERED=1`` in the child
environment if they need to observe that output before this process blocks on
stdin.  It has no dependency on the application package or platform utilities.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path
from types import FrameType
from typing import NoReturn


def _append_trace(trace: Path | None, token: str) -> None:
    if trace is None:
        return
    with trace.open("a", encoding="utf-8") as stream:
        stream.write(f"{token}\n")


def _install_signal_handlers(trace: Path | None, ignore_term: bool) -> None:
    def handle_signal(signum: int, _frame: FrameType | None) -> NoReturn | None:
        name = signal.Signals(signum).name
        suffix = ":ignored" if ignore_term else ""
        _append_trace(trace, f"hccast:signal:{name}{suffix}")
        if ignore_term:
            return None
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--capture", type=Path)
    parser.add_argument(
        "--mode",
        choices=(
            "normal",
            "exit-before-handshake",
            "eof-after-handshake",
            "malformed-json",
            "oversized-json",
        ),
        default="normal",
    )
    parser.add_argument("--exit-before-handshake", action="store_true")
    parser.add_argument("--eof-after-handshake", action="store_true")
    parser.add_argument("--malformed-json", action="store_true")
    parser.add_argument("--oversized-json", action="store_true")
    parser.add_argument("--oversized-bytes", type=int, default=65_537)
    parser.add_argument("--ignore-term", action="store_true")
    parser.add_argument("--read-size", type=int, default=65_536)
    return parser


def _selected_mode(args: argparse.Namespace) -> str:
    selected = [
        (args.exit_before_handshake, "exit-before-handshake"),
        (args.eof_after_handshake, "eof-after-handshake"),
        (args.malformed_json, "malformed-json"),
        (args.oversized_json, "oversized-json"),
    ]
    flag_modes = [mode for enabled, mode in selected if enabled]
    if len(flag_modes) > 1:
        raise SystemExit("choose at most one failure-mode flag")
    if flag_modes and args.mode != "normal":
        raise SystemExit("do not combine --mode with a failure-mode flag")
    return flag_modes[0] if flag_modes else str(args.mode)


def _emit_handshake(mode: str, oversized_bytes: int, trace: Path | None) -> None:
    print("Enumerating directly as Android Open Accessory 18d1:2d00...")
    _append_trace(trace, "hccast:direct-aoa")
    print("TX SETR device-info request", file=sys.stderr)
    _append_trace(trace, "hccast:setr")
    print("HCCAST handshake complete:")
    _append_trace(trace, "hccast:handshake-marker")

    if mode == "malformed-json":
        print('{"product": "HCT-AT01", "version": }')
        _append_trace(trace, "hccast:malformed-json")
        return

    handshake: dict[str, str] = {"product": "HCT-AT01", "version": "2505161526"}
    if mode == "oversized-json":
        if oversized_bytes < 1:
            raise SystemExit("--oversized-bytes must be positive")
        handshake["padding"] = "x" * oversized_bytes
        _append_trace(trace, "hccast:oversized-json")
    print(json.dumps(handshake, indent=2, sort_keys=True))
    _append_trace(trace, "hccast:handshake-json")


def _copy_stdin(capture: Path | None, read_size: int, trace: Path | None) -> None:
    if read_size < 1:
        raise SystemExit("--read-size must be positive")
    output = capture.open("wb") if capture is not None else None
    try:
        while True:
            chunk = sys.stdin.buffer.read(read_size)
            if not chunk:
                _append_trace(trace, "hccast:stdin-eof")
                return
            if output is not None:
                output.write(chunk)
    finally:
        if output is not None:
            output.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = _selected_mode(args)
    _install_signal_handlers(args.trace, args.ignore_term)
    _append_trace(args.trace, "hccast:start")
    try:
        if mode == "exit-before-handshake":
            _append_trace(args.trace, "hccast:exit-before-handshake")
            return 23
        _emit_handshake(mode, args.oversized_bytes, args.trace)
        if mode in {"eof-after-handshake", "malformed-json", "oversized-json"}:
            _append_trace(args.trace, "hccast:stdout-eof")
            return 0
        _copy_stdin(args.capture, args.read_size, args.trace)
        return 0
    finally:
        _append_trace(args.trace, "hccast:exit")


if __name__ == "__main__":
    raise SystemExit(main())
