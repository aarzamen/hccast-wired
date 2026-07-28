"""Command-line interface for the experimental HCCAST wired driver."""

from __future__ import annotations

import argparse
import json
import logging
import math
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path

from .functionfs import AOAIdentity, FunctionFSControl, FunctionFSTransport
from .gadget import (
    AOA_ACCESSORY_PID,
    AOA_VENDOR_ID,
    DEFAULT_PRE_AOA_PRODUCT_ID,
    DEFAULT_PRE_AOA_VENDOR_ID,
    ConfigFSGadget,
)
from .host_usb import HostUSBTransport, enumerate_candidates
from .protocol import ScreenInfo, Settings
from .session import HCCASTSession
from .setr_probe import SetrOnceClassification, run_setr_once

LOG = logging.getLogger(__name__)


def _int_auto(value: str) -> int:
    return int(value, 0)


def _bounded_hold_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--hold-seconds must be a number") from exc
    if not math.isfinite(seconds):
        raise argparse.ArgumentTypeError("--hold-seconds must be finite")
    if seconds < 0:
        raise argparse.ArgumentTypeError("--hold-seconds must be nonnegative")
    if seconds > 10:
        raise argparse.ArgumentTypeError("--hold-seconds must not exceed 10 seconds")
    return seconds


def _bounded_response_timeout_ms(value: str) -> int:
    try:
        timeout_ms = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "--response-timeout-ms must be an integer from 1 through 500"
        ) from exc
    if not 1 <= timeout_ms <= 500:
        raise argparse.ArgumentTypeError(
            "--response-timeout-ms must be between 1 and 500 milliseconds"
        )
    return timeout_ms


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )


def _print_device_info(info: object) -> None:
    print(json.dumps(asdict(info), indent=2, sort_keys=True))


def command_probe_host(_: argparse.Namespace) -> int:
    candidates = enumerate_candidates()
    if not candidates:
        print("No supported direct-host USB candidate found.")
        print("APK-derived: 05ac:12ad and abcd:0002")
        print(
            "hardware-observed pre-protocol candidate: 1cbe:0005 "
            "(TI assigns that VID:PID to its MSC example; protocol unverified until SETV)"
        )
        print("Try a USB-A-host-to-USB-C cable/adapter to force the monitor into peripheral mode.")
        return 2
    for candidate in candidates:
        print(json.dumps(asdict(candidate), sort_keys=True))
    return 0


def _screen_info(args: argparse.Namespace) -> ScreenInfo:
    orientation = 1 if args.orientation == "landscape" else 0
    source_short = min(args.source_width, args.source_height)
    source_long = max(args.source_width, args.source_height)
    return ScreenInfo(
        orientation=orientation,
        encoder_width=args.width,
        encoder_height=args.height,
        source_short_side=source_short,
        source_long_side=source_long,
    )


def _maybe_send_settings(session: HCCASTSession, args: argparse.Namespace) -> None:
    if not args.send_settings:
        return
    session.send_settings(
        Settings(
            mirror_resolution=args.mirror_resolution,
            vertical_mode=1 if args.orientation == "landscape" else 0,
            vertical_auto_revolve=0,
            full_mode=1,
        )
    )


def _run_session(transport: object, args: argparse.Namespace, *, stream: bool) -> int:
    session = HCCASTSession(transport)  # type: ignore[arg-type]
    try:
        info = session.handshake(timeout_s=args.handshake_timeout)
        print("HCCAST handshake complete:")
        _print_device_info(info)
        _maybe_send_settings(session, args)
        if stream:
            screen = _screen_info(args)
            session.send_screen_info(screen)
            if args.file == "-":
                if args.loop:
                    raise ValueError("--loop cannot be used with stdin")
                stats = session.stream_h264_stream(
                    sys.stdin.buffer,
                    fps=args.fps,
                    packetization=args.packetization,
                    max_packets=args.max_packets,
                )
            else:
                stats = session.stream_h264_file(
                    args.file,
                    fps=args.fps,
                    packetization=args.packetization,
                    loop=args.loop,
                    max_packets=args.max_packets,
                )
            print("Stream complete:")
            _print_device_info(stats)
        return 0
    finally:
        session.close()


def command_host_handshake(args: argparse.Namespace) -> int:
    transport = HostUSBTransport(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        interface_number=args.interface,
        detach_kernel=args.detach_kernel,
        try_claim_with_kernel_driver=args.try_claim_with_kernel_driver,
        wait_seconds=args.wait_seconds,
        poll_interval=args.poll_interval,
    )
    return _run_session(transport, args, stream=False)


def command_host_stream(args: argparse.Namespace) -> int:
    transport = HostUSBTransport(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        interface_number=args.interface,
        detach_kernel=args.detach_kernel,
        try_claim_with_kernel_driver=args.try_claim_with_kernel_driver,
        wait_seconds=args.wait_seconds,
        poll_interval=args.poll_interval,
    )
    return _run_session(transport, args, stream=True)


def command_host_claim(args: argparse.Namespace) -> int:
    """Claim/release without HCCAST application payload I/O or active reconfiguration."""

    transport = HostUSBTransport(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        interface_number=args.interface,
        detach_kernel=False,
        try_claim_with_kernel_driver=args.try_claim_with_kernel_driver,
        allow_configuration_activation=False,
        wait_seconds=args.wait_seconds,
        poll_interval=args.poll_interval,
    )
    try:
        if args.hold_seconds > 0:
            time.sleep(args.hold_seconds)
            print(
                f"Requested {args.hold_seconds:.3f}-second observation window elapsed; "
                "releasing now. This does not establish that the device remained "
                "attached during that window. No configuration activation or "
                "kernel-driver detachment was requested, and no HCCAST/application "
                "bulk-endpoint payload I/O was performed."
            )
        else:
            print(
                "USB interface claim succeeded; releasing immediately; no configuration "
                "activation or kernel-driver detachment was requested, and no "
                "HCCAST/application bulk-endpoint payload I/O was performed."
            )
        return 0
    finally:
        transport.close()


def command_host_setr_once(args: argparse.Namespace) -> int:
    """Send one SETR and preserve bounded raw response bytes before parsing."""

    transport = HostUSBTransport(
        vendor_id=args.vendor_id,
        product_id=args.product_id,
        interface_number=args.interface,
        detach_kernel=False,
        try_claim_with_kernel_driver=args.try_claim_with_kernel_driver,
        allow_configuration_activation=False,
        wait_seconds=args.wait_seconds,
        poll_interval=args.poll_interval,
    )
    try:
        result = run_setr_once(
            transport,
            raw_output=Path(args.raw_output),
            response_window_ms=args.response_timeout_ms,
        )
        print(json.dumps(result.to_json_dict(), indent=2, sort_keys=True))
        if result.classification is SetrOnceClassification.SETR_WRITE_FAILED:
            # The JSON remains the authoritative structured result, while a distinct
            # status ensures shell callers cannot mistake a failed OUT transfer for a
            # successful probe.
            return 3
        return 0
    finally:
        transport.close()


def _print_aoa_identity(identity: AOAIdentity) -> None:
    print("AOA identity sent by the display/accessory host:")
    print(json.dumps(asdict(identity), indent=2, sort_keys=True))


def _run_gadget(args: argparse.Namespace, *, stream: bool) -> int:
    gadget = ConfigFSGadget(
        name=args.gadget_name,
        function_name=args.function_name,
        mountpoint=args.mountpoint,
        udc=args.udc,
    )
    control: FunctionFSControl | None = None
    transport: FunctionFSTransport | None = None
    session: HCCASTSession | None = None

    interrupted = False

    def handle_signal(signum: int, frame: object) -> None:
        nonlocal interrupted
        interrupted = True
        LOG.warning("received signal %d; cleaning up", signum)
        raise KeyboardInterrupt

    previous_sigint = signal.signal(signal.SIGINT, handle_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, handle_signal)

    try:
        negotiate = args.aoa_mode == "negotiate"
        if negotiate:
            vendor_id = args.pre_aoa_vendor_id
            product_id = args.pre_aoa_product_id
            manufacturer = "Linux"
            product = "Android-compatible HCCAST source"
        else:
            vendor_id = AOA_VENDOR_ID
            product_id = AOA_ACCESSORY_PID
            manufacturer = "Android"
            product = "Android Accessory"

        gadget.create(
            vendor_id=vendor_id,
            product_id=product_id,
            manufacturer=manufacturer,
            product=product,
            serial=args.serial,
        )
        gadget.mount_functionfs()
        control = FunctionFSControl(
            args.mountpoint,
            receive_all_control=negotiate,
        )
        control.open()
        gadget.link_function()
        gadget.bind()

        if negotiate:
            print("Waiting for the screen to issue Android Open Accessory requests 51/52/53...")
            result = control.negotiate_aoa(timeout_s=args.aoa_timeout)
            _print_aoa_identity(result.identity)
            if not result.started:
                raise RuntimeError(
                    "screen did not send AOA START_ACCESSORY. Try --aoa-mode direct, "
                    "verify the data port/cable, or inspect kernel support."
                )
            gadget.reenumerate_as_aoa(disconnect_s=args.reenumeration_delay)
            control.wait_for_enable(timeout_s=args.enable_timeout)
        else:
            print("Enumerating directly as Android Open Accessory 18d1:2d00...")
            control.wait_for_enable(timeout_s=args.enable_timeout)

        transport = FunctionFSTransport(args.mountpoint)
        session = HCCASTSession(transport)
        info = session.handshake(timeout_s=args.handshake_timeout)
        print("HCCAST handshake complete:")
        _print_device_info(info)
        _maybe_send_settings(session, args)

        if stream:
            session.send_screen_info(_screen_info(args))
            if args.file == "-":
                if args.loop:
                    raise ValueError("--loop cannot be used with stdin")
                stats = session.stream_h264_stream(
                    sys.stdin.buffer,
                    fps=args.fps,
                    packetization=args.packetization,
                    max_packets=args.max_packets,
                )
            else:
                stats = session.stream_h264_file(
                    args.file,
                    fps=args.fps,
                    packetization=args.packetization,
                    loop=args.loop,
                    max_packets=args.max_packets,
                )
            print("Stream complete:")
            _print_device_info(stats)
        return 0
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)
        if session is not None:
            session.close()
            transport = None
        elif transport is not None:
            transport.close()
        if control is not None:
            control.close()
        gadget.cleanup()
        if interrupted:
            LOG.info("interrupted cleanly")


def command_gadget_handshake(args: argparse.Namespace) -> int:
    return _run_gadget(args, stream=False)


def command_gadget_stream(args: argparse.Namespace) -> int:
    return _run_gadget(args, stream=True)


def command_gadget_stop(args: argparse.Namespace) -> int:
    ConfigFSGadget.force_cleanup(
        name=args.gadget_name,
        function_name=args.function_name,
        mountpoint=args.mountpoint,
    )
    return 0


def add_common_session_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--handshake-timeout", type=float, default=30.0)
    parser.add_argument("--send-settings", action="store_true")
    parser.add_argument("--mirror-resolution", type=int, choices=range(4), default=1)
    parser.add_argument("--orientation", choices=("portrait", "landscape"), default="landscape")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--source-width", type=int, default=1280)
    parser.add_argument("--source-height", type=int, default=720)


def add_stream_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", help="Annex-B H.264 file, or - for stdin")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--packetization", choices=("access-unit", "nal"), default="access-unit")
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--max-packets", type=int)


def add_host_discovery_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--vendor-id", type=_int_auto)
    parser.add_argument("--product-id", type=_int_auto)
    parser.add_argument("--interface", type=int)
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="wait this long for a transient USB device (default: one-shot scan)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=0.02,
        help="seconds between USB discovery scans while waiting (default: 0.02)",
    )


def add_try_claim_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--try-claim-with-kernel-driver",
        action="store_true",
        help="attempt a normal libusb claim without detaching an active kernel driver",
    )


def add_host_options(parser: argparse.ArgumentParser) -> None:
    add_host_discovery_options(parser)
    parser.add_argument("--detach-kernel", action="store_true")
    add_try_claim_option(parser)
    add_common_session_options(parser)


def add_gadget_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--aoa-mode", choices=("negotiate", "direct"), default="negotiate")
    parser.add_argument("--aoa-timeout", type=float, default=30.0)
    parser.add_argument("--enable-timeout", type=float, default=30.0)
    parser.add_argument("--reenumeration-delay", type=float, default=0.35)
    parser.add_argument("--pre-aoa-vendor-id", type=_int_auto, default=DEFAULT_PRE_AOA_VENDOR_ID)
    parser.add_argument("--pre-aoa-product-id", type=_int_auto, default=DEFAULT_PRE_AOA_PRODUCT_ID)
    parser.add_argument("--udc")
    parser.add_argument("--mountpoint", default="/dev/ffs-hccast")
    parser.add_argument("--gadget-name", default="hccast")
    parser.add_argument("--function-name", default="hccast")
    parser.add_argument("--serial", default="HCCAST-LINUX-001")
    add_common_session_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hccast-wired",
        description="Experimental wired driver for DrongScreen/HCCAST selfie monitors",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    sub = parser.add_subparsers(dest="command", required=True)

    probe = sub.add_parser(
        "probe-host",
        help=(
            "look for APK-derived 05ac:12ad/abcd:0002 or hardware-observed "
            "pre-protocol 1cbe:0005 (TI MSC-assigned; protocol unverified)"
        ),
    )
    probe.set_defaults(func=command_probe_host)

    host_claim = sub.add_parser(
        "host-claim",
        help=(
            "claim/release only: no detach; no HCCAST/application bulk payload I/O"
        ),
        description=(
            "Diagnostic claim/release only: no kernel-driver detach, no configuration "
            "activation, and no HCCAST/application bulk-endpoint payload I/O. USB "
            "enumeration, descriptor, and control machinery still exists below this "
            "application boundary."
        ),
    )
    add_host_discovery_options(host_claim)
    host_claim.add_argument(
        "--hold-seconds",
        type=_bounded_hold_seconds,
        default=0.0,
        help=(
            "bounded claim observation window in seconds; completion does not prove "
            "attachment survival (default: 0; maximum: 10)"
        ),
    )
    add_try_claim_option(host_claim)
    host_claim.set_defaults(func=command_host_claim)

    host_setr_once = sub.add_parser(
        "host-setr-once",
        help="send exactly one SETR and preserve at most 500 ms of raw response bytes",
        description=(
            "Safety-bounded identity probe: no kernel-driver detach, no configuration "
            "activation, exactly one 20-byte SETR, no retry, and no settings, audio, "
            "video, firmware, storage, or network operation. Raw response bytes are "
            "written to --raw-output before parsing."
        ),
    )
    add_host_discovery_options(host_setr_once)
    add_try_claim_option(host_setr_once)
    host_setr_once.add_argument(
        "--response-timeout-ms",
        type=_bounded_response_timeout_ms,
        default=500,
        help="bounded bulk-IN collection window in milliseconds (default/max: 500)",
    )
    host_setr_once.add_argument(
        "--raw-output",
        required=True,
        help="required path for raw response bytes; written before any parsing",
    )
    host_setr_once.set_defaults(func=command_host_setr_once)

    host_handshake = sub.add_parser("host-handshake", help="HCCAST handshake in USB host mode")
    add_host_options(host_handshake)
    host_handshake.set_defaults(func=command_host_handshake)

    host_stream = sub.add_parser("host-stream", help="stream H.264 to a USB-peripheral monitor")
    add_host_options(host_stream)
    add_stream_options(host_stream)
    host_stream.set_defaults(func=command_host_stream)

    gadget_handshake = sub.add_parser(
        "gadget-handshake",
        help="impersonate Android over USB gadget mode and perform HCCAST handshake",
    )
    add_gadget_options(gadget_handshake)
    gadget_handshake.set_defaults(func=command_gadget_handshake)

    gadget_stream = sub.add_parser(
        "gadget-stream",
        help="impersonate Android and stream an Annex-B H.264 file",
    )
    add_gadget_options(gadget_stream)
    add_stream_options(gadget_stream)
    gadget_stream.set_defaults(func=command_gadget_stream)

    stop = sub.add_parser("gadget-stop", help="remove a stale hccast ConfigFS gadget")
    stop.add_argument("--mountpoint", default="/dev/ffs-hccast")
    stop.add_argument("--gadget-name", default="hccast")
    stop.add_argument("--function-name", default="hccast")
    stop.set_defaults(func=command_gadget_stop)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        LOG.exception("command failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
