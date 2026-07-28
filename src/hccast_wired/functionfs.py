"""Linux FunctionFS support for the gadget-side HCCAST transport.

This module builds an Android Open Accessory-compatible USB function with one
vendor-specific interface and two bulk endpoints.  It supports two strategies:

``direct``
    Enumerate immediately as Google VID/PID 18d1:2d00.  The official AOA host
    flow permits a host to skip the 51/52/53 negotiation when it sees a device
    already in accessory mode.

``negotiate``
    Enumerate first as a generic USB device, receive AOA vendor requests 51/52/53
    through FunctionFS ep0, then disconnect and re-enumerate as 18d1:2d00.

The second mode more closely impersonates a real Android device and is preferred
when the target kernel supports FUNCTIONFS_ALL_CTRL_RECIP and
FUNCTIONFS_CONFIG0_SETUP.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import struct
import time
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from .transport import Transport, TransportError

LOG = logging.getLogger(__name__)

# linux/usb/functionfs.h
FUNCTIONFS_STRINGS_MAGIC = 2
FUNCTIONFS_DESCRIPTORS_MAGIC_V2 = 3
FUNCTIONFS_HAS_FS_DESC = 1
FUNCTIONFS_HAS_HS_DESC = 2
FUNCTIONFS_ALL_CTRL_RECIP = 64
FUNCTIONFS_CONFIG0_SETUP = 128

# USB descriptor constants
USB_DT_INTERFACE = 4
USB_DT_ENDPOINT = 5
USB_ENDPOINT_XFER_BULK = 2
USB_CLASS_VENDOR_SPEC = 0xFF

# AOA control requests
AOA_GET_PROTOCOL = 51
AOA_SEND_STRING = 52
AOA_START_ACCESSORY = 53
AOA_PROTOCOL_VERSION = 2

# bmRequestType fields
USB_DIR_IN = 0x80
USB_TYPE_VENDOR = 0x40
USB_RECIP_DEVICE = 0x00

_EVENT = struct.Struct("<BBHHHB3x")
_CTRL = struct.Struct("<BBHHH")


class FunctionFSError(TransportError):
    pass


class EventType(IntEnum):
    BIND = 0
    UNBIND = 1
    ENABLE = 2
    DISABLE = 3
    SETUP = 4
    SUSPEND = 5
    RESUME = 6


@dataclass(frozen=True, slots=True)
class ControlRequest:
    request_type: int
    request: int
    value: int
    index: int
    length: int

    @property
    def is_in(self) -> bool:
        return bool(self.request_type & USB_DIR_IN)


@dataclass(frozen=True, slots=True)
class FunctionFSEvent:
    event_type: EventType
    setup: ControlRequest | None = None


@dataclass(slots=True)
class AOAIdentity:
    manufacturer: str = ""
    model: str = ""
    description: str = ""
    version: str = ""
    uri: str = ""
    serial: str = ""

    def set_index(self, index: int, value: str) -> None:
        names = (
            "manufacturer",
            "model",
            "description",
            "version",
            "uri",
            "serial",
        )
        if 0 <= index < len(names):
            setattr(self, names[index], value)


@dataclass(frozen=True, slots=True)
class AOAResult:
    started: bool
    identity: AOAIdentity


def _interface_descriptor() -> bytes:
    return struct.pack(
        "<BBBBBBBBB",
        9,  # bLength
        USB_DT_INTERFACE,
        0,  # bInterfaceNumber (FunctionFS remaps if needed)
        0,  # bAlternateSetting
        2,  # bNumEndpoints
        USB_CLASS_VENDOR_SPEC,
        0xFF,
        0x00,
        1,  # iInterface
    )


def _endpoint_descriptor(address: int, max_packet: int) -> bytes:
    return struct.pack(
        "<BBBBHB",
        7,
        USB_DT_ENDPOINT,
        address,
        USB_ENDPOINT_XFER_BULK,
        max_packet,
        0,
    )


def build_descriptors(*, receive_all_control: bool = False) -> bytes:
    """Build FunctionFS v2 FS+HS descriptors.

    Endpoint declaration order is OUT then IN, so FunctionFS exposes them as
    ``ep1`` (host -> device) and ``ep2`` (device -> host).
    """

    flags = FUNCTIONFS_HAS_FS_DESC | FUNCTIONFS_HAS_HS_DESC
    if receive_all_control:
        flags |= FUNCTIONFS_ALL_CTRL_RECIP | FUNCTIONFS_CONFIG0_SETUP

    full_speed = b"".join(
        (
            _interface_descriptor(),
            _endpoint_descriptor(0x01, 64),
            _endpoint_descriptor(0x82, 64),
        )
    )
    high_speed = b"".join(
        (
            _interface_descriptor(),
            _endpoint_descriptor(0x01, 512),
            _endpoint_descriptor(0x82, 512),
        )
    )

    # V2 header, flags, fs_count, hs_count, then descriptors.
    length = 12 + 4 + 4 + len(full_speed) + len(high_speed)
    return (
        struct.pack(
            "<IIIII",
            FUNCTIONFS_DESCRIPTORS_MAGIC_V2,
            length,
            flags,
            3,
            3,
        )
        + full_speed
        + high_speed
    )


def build_strings(interface_name: str = "Android Accessory Interface") -> bytes:
    encoded = interface_name.encode("utf-8") + b"\x00"
    length = 16 + 2 + len(encoded)
    return struct.pack(
        "<IIIIH",
        FUNCTIONFS_STRINGS_MAGIC,
        length,
        1,  # str_count
        1,  # lang_count
        0x0409,  # en-US
    ) + encoded


class FunctionFSControl:
    """Owns FunctionFS ep0 and handles descriptors, events, and AOA requests."""

    def __init__(
        self,
        mountpoint: str | Path,
        *,
        receive_all_control: bool = False,
        interface_name: str = "Android Accessory Interface",
    ) -> None:
        self.mountpoint = Path(mountpoint)
        self.ep0_path = self.mountpoint / "ep0"
        self.receive_all_control = receive_all_control
        self.interface_name = interface_name
        self.fd: int | None = None

    def open(self) -> None:
        if self.fd is not None:
            return
        try:
            self.fd = os.open(self.ep0_path, os.O_RDWR)
            os.write(
                self.fd,
                build_descriptors(receive_all_control=self.receive_all_control),
            )
            os.write(self.fd, build_strings(self.interface_name))
        except OSError as exc:
            self.close()
            if exc.errno == errno.ENOSYS and self.receive_all_control:
                raise FunctionFSError(
                    "kernel rejected FunctionFS ALL_CTRL_RECIP/CONFIG0_SETUP; "
                    "use direct AOA mode or a Raw Gadget/f_accessory fallback"
                ) from exc
            raise FunctionFSError(f"cannot initialize FunctionFS ep0: {exc}") from exc
        LOG.info("FunctionFS descriptors and strings registered at %s", self.ep0_path)

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

    def read_events(self, *, timeout_ms: int | None = None) -> list[FunctionFSEvent]:
        if self.fd is None:
            raise FunctionFSError("FunctionFS ep0 is not open")
        poller = select.poll()
        poller.register(self.fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        timeout = -1 if timeout_ms is None else timeout_ms
        ready = poller.poll(timeout)
        if not ready:
            return []
        try:
            raw = os.read(self.fd, _EVENT.size * 16)
        except InterruptedError:
            return []
        except OSError as exc:
            raise FunctionFSError(f"FunctionFS ep0 event read failed: {exc}") from exc
        if not raw:
            raise FunctionFSError("FunctionFS ep0 closed")
        if len(raw) % _EVENT.size != 0:
            raise FunctionFSError(f"short/misaligned FunctionFS event block: {len(raw)} bytes")

        events: list[FunctionFSEvent] = []
        for offset in range(0, len(raw), _EVENT.size):
            request_type, request, value, index, length, event_value = _EVENT.unpack_from(
                raw, offset
            )
            try:
                event_type = EventType(event_value)
            except ValueError:
                LOG.warning("unknown FunctionFS event type %d", event_value)
                continue
            setup = None
            if event_type is EventType.SETUP:
                setup = ControlRequest(request_type, request, value, index, length)
            events.append(FunctionFSEvent(event_type, setup))
        return events

    def _control_read(self, length: int) -> bytes:
        if self.fd is None:
            raise FunctionFSError("FunctionFS ep0 is not open")
        data = bytearray()
        while len(data) < length:
            chunk = os.read(self.fd, length - len(data))
            if not chunk:
                raise FunctionFSError("control OUT data stage ended early")
            data.extend(chunk)
        return bytes(data)

    def _control_write(self, data: bytes) -> None:
        if self.fd is None:
            raise FunctionFSError("FunctionFS ep0 is not open")
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            written = os.write(self.fd, view[sent:])
            if written <= 0:
                raise FunctionFSError("control IN data stage made no progress")
            sent += written
        if not data:
            # A zero-length write acknowledges a no-data or zero-length IN transfer.
            os.write(self.fd, b"")

    def stall_setup(self, setup: ControlRequest) -> None:
        """Deliberately fail an unsupported setup request.

        FunctionFS stalls a setup transaction when the data-stage operation fails.
        A zero-byte read/write against a non-zero expected length commonly yields
        the intended stall.  Here we use an invalid-direction operation and log it;
        this path should not be reached for the known AOA requests.
        """

        LOG.warning(
            "stalling unsupported control request type=0x%02x request=%d value=%d "
            "index=%d length=%d",
            setup.request_type,
            setup.request,
            setup.value,
            setup.index,
            setup.length,
        )
        if setup.is_in:
            self._control_write(b"")
        elif setup.length:
            # Consume and ignore OUT data to leave ep0 synchronized.  This ACKs rather
            # than hard-stalls on some kernels, but is safer than desynchronizing ep0.
            self._control_read(setup.length)
        else:
            self._control_write(b"")

    def handle_aoa_setup(self, setup: ControlRequest, identity: AOAIdentity) -> bool:
        """Handle one AOA request; return True after START_ACCESSORY."""

        vendor_device = (setup.request_type & 0x7F) == (USB_TYPE_VENDOR | USB_RECIP_DEVICE)
        if not vendor_device:
            self.stall_setup(setup)
            return False

        if setup.request == AOA_GET_PROTOCOL and setup.is_in:
            response = struct.pack("<H", AOA_PROTOCOL_VERSION)
            self._control_write(response[: setup.length])
            LOG.info("AOA GET_PROTOCOL -> %d", AOA_PROTOCOL_VERSION)
            return False

        if setup.request == AOA_SEND_STRING and not setup.is_in:
            raw = self._control_read(setup.length) if setup.length else b""
            value = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
            identity.set_index(setup.index, value)
            LOG.info("AOA string[%d] = %r", setup.index, value)
            return False

        if setup.request == AOA_START_ACCESSORY and not setup.is_in:
            if setup.length:
                self._control_read(setup.length)
            else:
                self._control_write(b"")
            LOG.info("AOA START_ACCESSORY received")
            return True

        self.stall_setup(setup)
        return False

    def wait_for_enable(self, *, timeout_s: float = 30.0) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            for event in self.read_events(timeout_ms=min(remaining_ms, 1000)):
                LOG.debug("FunctionFS event: %s", event.event_type.name)
                if event.event_type is EventType.ENABLE:
                    return
                if event.event_type is EventType.SETUP and event.setup is not None:
                    self.stall_setup(event.setup)
        raise FunctionFSError("timed out waiting for FunctionFS ENABLE")

    def negotiate_aoa(self, *, timeout_s: float = 30.0) -> AOAResult:
        identity = AOAIdentity()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            for event in self.read_events(timeout_ms=min(remaining_ms, 1000)):
                LOG.debug("FunctionFS event: %s", event.event_type.name)
                if event.event_type is EventType.SETUP and event.setup is not None:
                    if self.handle_aoa_setup(event.setup, identity):
                        return AOAResult(True, identity)
        return AOAResult(False, identity)


class FunctionFSTransport(Transport):
    """Bulk endpoint transport after FunctionFS has been enabled by the host."""

    def __init__(
        self,
        mountpoint: str | Path,
        *,
        write_chunk: int = 16_384,
    ) -> None:
        self.mountpoint = Path(mountpoint)
        self.write_chunk = write_chunk
        # Declaration order in build_descriptors: OUT then IN.
        self.out_path = self.mountpoint / "ep1"  # host -> Linux device; read here
        self.in_path = self.mountpoint / "ep2"  # Linux device -> host; write here
        self._out_fd: int | None = None
        self._in_fd: int | None = None
        self._open_endpoints()

    def _open_endpoints(self) -> None:
        try:
            self._out_fd = os.open(self.out_path, os.O_RDONLY | os.O_NONBLOCK)
            self._in_fd = os.open(self.in_path, os.O_WRONLY)
        except OSError as exc:
            self.close()
            raise FunctionFSError(
                f"cannot open FunctionFS data endpoints at {self.mountpoint}: {exc}"
            ) from exc
        LOG.info("FunctionFS bulk OUT=%s bulk IN=%s", self.out_path, self.in_path)

    def write(self, data: bytes) -> None:
        if self._in_fd is None:
            raise FunctionFSError("FunctionFS transport is closed")
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            chunk = view[offset : offset + self.write_chunk]
            try:
                written = os.write(self._in_fd, chunk)
            except OSError as exc:
                raise FunctionFSError(f"FunctionFS bulk IN write failed: {exc}") from exc
            if written <= 0:
                raise FunctionFSError("FunctionFS bulk IN write made no progress")
            offset += written

    def read(self, *, timeout_ms: int = 500) -> bytes:
        if self._out_fd is None:
            raise FunctionFSError("FunctionFS transport is closed")
        poller = select.poll()
        poller.register(self._out_fd, select.POLLIN | select.POLLERR | select.POLLHUP)
        ready = poller.poll(timeout_ms)
        if not ready:
            return b""
        try:
            return os.read(self._out_fd, 16_384)
        except BlockingIOError:
            return b""
        except OSError as exc:
            raise FunctionFSError(f"FunctionFS bulk OUT read failed: {exc}") from exc

    def close(self) -> None:
        for name in ("_out_fd", "_in_fd"):
            fd = getattr(self, name)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, name, None)
