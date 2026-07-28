#!/usr/bin/env python3
"""Local stdlib-only fake for encoders and blocking display/source children."""

from __future__ import annotations

import argparse
import signal
import socket
import sys
import time
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
        _append_trace(trace, f"encoder:signal:{name}{suffix}")
        if ignore_term:
            return None
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path)
    parser.add_argument("--payload-hex", "--data-hex", dest="payload_hex", default="")
    parser.add_argument("--payload-file", "--data-file", dest="payload_file", type=Path)
    parser.add_argument("--diagnostic", default="encoder diagnostic")
    parser.add_argument(
        "--mode",
        choices=("once", "fail-before-output", "close-stdout-early", "block", "repeat"),
        default="once",
    )
    parser.add_argument("--fail-before-output", action="store_true")
    parser.add_argument("--close-stdout-early", action="store_true")
    parser.add_argument("--block", action="store_true")
    parser.add_argument("--repeat", action="store_true")
    parser.add_argument("--repeat-count", type=int)
    parser.add_argument("--repeat-interval", type=float, default=0.01)
    parser.add_argument("--role")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--ignore-term", action="store_true")
    return parser


def _selected_mode(args: argparse.Namespace) -> str:
    selected = [
        (args.fail_before_output, "fail-before-output"),
        (args.close_stdout_early, "close-stdout-early"),
        (args.block, "block"),
        (args.repeat, "repeat"),
    ]
    flag_modes = [mode for enabled, mode in selected if enabled]
    if len(flag_modes) > 1:
        raise SystemExit("choose at most one mode flag")
    if flag_modes and args.mode != "once":
        raise SystemExit("do not combine --mode with a mode flag")
    return flag_modes[0] if flag_modes else str(args.mode)


def _payload(args: argparse.Namespace) -> bytes:
    if args.payload_file is not None and args.payload_hex:
        raise SystemExit("choose one of --payload-file and --payload-hex")
    if args.payload_file is not None:
        return bytes(args.payload_file.read_bytes())
    try:
        return bytes.fromhex(args.payload_hex)
    except ValueError as error:
        raise SystemExit(f"invalid --payload-hex: {error}") from error


def _block_forever() -> None:
    while True:
        time.sleep(0.05)


def _make_socket(path: Path, trace: Path | None) -> socket.socket:
    if path.exists() or path.is_symlink():
        raise SystemExit(f"refusing to replace existing socket path: {path}")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
    except BaseException:
        listener.close()
        raise
    _append_trace(trace, "encoder:socket-created")
    return listener


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    mode = _selected_mode(args)
    _install_signal_handlers(args.trace, args.ignore_term)
    _append_trace(args.trace, "encoder:start")
    listener: socket.socket | None = None
    try:
        if args.role is not None:
            _append_trace(args.trace, f"encoder:role:{args.role}")
        if args.socket is not None:
            listener = _make_socket(args.socket, args.trace)
        if args.diagnostic:
            print(args.diagnostic, file=sys.stderr)
        if mode == "fail-before-output":
            _append_trace(args.trace, "encoder:fail-before-output")
            return 31
        if args.role is not None:
            _block_forever()

        payload = _payload(args)
        if mode == "close-stdout-early":
            sys.stdout.close()
            _append_trace(args.trace, "encoder:stdout-closed")
            return 0

        if mode == "repeat":
            count = 0
            while args.repeat_count is None or count < args.repeat_count:
                sys.stdout.buffer.write(payload)
                sys.stdout.buffer.flush()
                count += 1
                if args.repeat_interval > 0:
                    time.sleep(args.repeat_interval)
            _append_trace(args.trace, f"encoder:output:{count}")
        else:
            sys.stdout.buffer.write(payload)
            sys.stdout.buffer.flush()
            _append_trace(args.trace, "encoder:output:1")
        if mode == "block":
            _block_forever()
        return 0
    finally:
        if listener is not None:
            socket_path = Path(listener.getsockname())
            listener.close()
            try:
                socket_path.unlink()
            except FileNotFoundError:
                pass
            _append_trace(args.trace, "encoder:socket-removed")
        _append_trace(args.trace, "encoder:exit")


if __name__ == "__main__":
    raise SystemExit(main())
