"""Direct host-side libusb backend for HCCAST's USB-device personality.

The factory APK contains two device filters:

* 05ac:12ad
* abcd:0002

Physical testing also observed a transient direct-host device with a vendor-specific
bulk IN/OUT interface under this identity:

* 1cbe:0005

Texas Instruments assigns that VID/PID to an MSC example, while the observed interface
was anomalous `ff/06/50`. It is therefore a pre-protocol candidate only, not an
HCCAST-proven identity. Its application protocol remains unverified until a separately
authorized exchange returns HCCAST `SETV`.

Some HCCAST hardware can therefore be the USB peripheral while Android is the
host.  This backend is for that mode.  It deliberately does *not* perform AOA
requests: AOA is the opposite USB role, where the screen is host and Android is
the peripheral.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Any, Final

from .transport import Transport, TransportError

LOG = logging.getLogger(__name__)

APK_DERIVED_CANDIDATE_IDS: Final[tuple[tuple[int, int], ...]] = (
    (0x05AC, 0x12AD),
    (0xABCD, 0x0002),
)
HARDWARE_OBSERVED_CANDIDATE_IDS: Final[tuple[tuple[int, int], ...]] = (
    # Transient ff/06/50 bulk interface under a TI MSC-assigned identity. Protocol unknown.
    (0x1CBE, 0x0005),
)
CANDIDATE_IDS: Final[tuple[tuple[int, int], ...]] = (
    APK_DERIVED_CANDIDATE_IDS + HARDWARE_OBSERVED_CANDIDATE_IDS
)


@dataclass(frozen=True, slots=True)
class USBDeviceSummary:
    vendor_id: int
    product_id: int
    manufacturer: str
    product: str
    serial: str

    @property
    def vid_pid(self) -> str:
        return f"{self.vendor_id:04x}:{self.product_id:04x}"


def _load_usb() -> tuple[Any, Any]:
    try:
        import usb.core  # type: ignore[import-not-found]
        import usb.util  # type: ignore[import-not-found]
    except ImportError as exc:
        raise TransportError(
            "PyUSB is not installed. In a source checkout, run `uv sync --extra host`. "
            "For installed distributions, select the optional `host` extra."
        ) from exc
    return usb.core, usb.util


def _safe_string(util: Any, dev: Any, index: int) -> str:
    if not index:
        return ""
    try:
        return util.get_string(dev, index) or ""
    except Exception:
        return ""


def enumerate_candidates() -> list[USBDeviceSummary]:
    core, util = _load_usb()
    found: list[USBDeviceSummary] = []
    for vendor_id, product_id in CANDIDATE_IDS:
        for dev in core.find(find_all=True, idVendor=vendor_id, idProduct=product_id) or []:
            found.append(
                USBDeviceSummary(
                    vendor_id=dev.idVendor,
                    product_id=dev.idProduct,
                    manufacturer=_safe_string(util, dev, dev.iManufacturer),
                    product=_safe_string(util, dev, dev.iProduct),
                    serial=_safe_string(util, dev, dev.iSerialNumber),
                )
            )
    return found


class HostUSBTransport(Transport):
    """PyUSB transport for a monitor that enumerates as a USB peripheral."""

    def __init__(
        self,
        *,
        vendor_id: int | None = None,
        product_id: int | None = None,
        interface_number: int | None = None,
        detach_kernel: bool = False,
        try_claim_with_kernel_driver: bool = False,
        allow_configuration_activation: bool = True,
        write_chunk: int = 16_384,
        timeout_ms: int = 1_000,
        wait_seconds: float = 0.0,
        poll_interval: float = 0.02,
    ) -> None:
        if detach_kernel and try_claim_with_kernel_driver:
            raise ValueError(
                "detach_kernel and try_claim_with_kernel_driver cannot both be enabled"
            )
        if not math.isfinite(wait_seconds):
            raise ValueError("wait_seconds must be finite")
        if wait_seconds < 0:
            raise ValueError("wait_seconds must be non-negative")
        if not math.isfinite(poll_interval):
            raise ValueError("poll_interval must be finite")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.vendor_id = vendor_id
        self.product_id = product_id
        self.interface_number = interface_number
        self.detach_kernel = detach_kernel
        self.try_claim_with_kernel_driver = try_claim_with_kernel_driver
        self.allow_configuration_activation = allow_configuration_activation
        self.write_chunk = write_chunk
        self.timeout_ms = timeout_ms
        self.wait_seconds = wait_seconds
        self.poll_interval = poll_interval
        self._core, self._util = _load_usb()
        self._dev: Any = None
        self._interface: Any = None
        self._ep_in: Any = None
        self._ep_out: Any = None
        self._detached_interface_number: int | None = None
        self._open()

    def _find_device_once(self) -> Any | None:
        if self.vendor_id is not None or self.product_id is not None:
            if self.vendor_id is None or self.product_id is None:
                raise ValueError("vendor_id and product_id must be supplied together")
            return self._core.find(idVendor=self.vendor_id, idProduct=self.product_id)

        for vendor_id, product_id in CANDIDATE_IDS:
            dev = self._core.find(idVendor=vendor_id, idProduct=product_id)
            if dev is not None:
                return dev
        return None

    def _not_found_error(self) -> TransportError:
        if self.vendor_id is not None and self.product_id is not None:
            return TransportError(
                f"USB device {self.vendor_id:04x}:{self.product_id:04x} not found"
            )
        return TransportError(
            "No supported direct-host USB candidate found "
            "(APK-derived: 05ac:12ad and abcd:0002; "
            "hardware-observed pre-protocol candidate: 1cbe:0005 "
            "(TI MSC-assigned identity; protocol unverified until SETV))"
        )

    def _find_device(self) -> Any:
        # Keep the original one-shot path exact: no clock read and no sleep.
        if self.wait_seconds == 0:
            dev = self._find_device_once()
            if dev is None:
                raise self._not_found_error()
            return dev

        deadline = time.monotonic() + self.wait_seconds
        while True:
            dev = self._find_device_once()
            if dev is not None:
                return dev
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise self._not_found_error()
            time.sleep(min(self.poll_interval, remaining))

    def _active_configuration(self, dev: Any) -> Any:
        try:
            return dev.get_active_configuration()
        except Exception as initial_exc:
            if not self.allow_configuration_activation:
                raise TransportError(
                    "claim-only safety: active USB configuration lookup failed and "
                    "configuration activation is disabled; refusing to request "
                    f"SET_CONFIGURATION: {initial_exc}"
                ) from initial_exc
            try:
                dev.set_configuration()
            except Exception as activation_exc:
                raise TransportError(
                    "cannot activate USB configuration; initial active-configuration "
                    f"lookup failed: {initial_exc}; activation failed: {activation_exc}"
                ) from activation_exc
            try:
                return dev.get_active_configuration()
            except Exception as retry_exc:
                raise TransportError(
                    "cannot obtain active USB configuration after activation; initial "
                    f"lookup failed: {initial_exc}; retry failed: {retry_exc}"
                ) from retry_exc

    def _open(self) -> None:
        dev = self._find_device()
        self._dev = dev

        try:
            self._open_found_device(dev)
        except BaseException:
            self.close()
            raise

    def _open_found_device(self, dev: Any) -> None:
        config = self._active_configuration(dev)

        selected = None
        ep_in = None
        ep_out = None
        for interface in config:
            if (
                self.interface_number is not None
                and interface.bInterfaceNumber != self.interface_number
            ):
                continue
            candidate_in = None
            candidate_out = None
            for endpoint in interface:
                if self._util.endpoint_type(endpoint.bmAttributes) != self._util.ENDPOINT_TYPE_BULK:
                    continue
                direction = self._util.endpoint_direction(endpoint.bEndpointAddress)
                if direction == self._util.ENDPOINT_IN:
                    candidate_in = endpoint
                else:
                    candidate_out = endpoint
            if candidate_in is not None and candidate_out is not None:
                selected = interface
                ep_in = candidate_in
                ep_out = candidate_out
                break

        if selected is None or ep_in is None or ep_out is None:
            raise TransportError("no interface with both bulk IN and bulk OUT endpoints found")

        interface_number = selected.bInterfaceNumber
        non_detaching_claim = False
        try:
            if dev.is_kernel_driver_active(interface_number):
                if self.detach_kernel:
                    dev.detach_kernel_driver(interface_number)
                    self._detached_interface_number = interface_number
                elif self.try_claim_with_kernel_driver:
                    non_detaching_claim = True
                    LOG.warning(
                        "kernel driver reports interface %d active; attempting "
                        "explicit non-detaching claim",
                        interface_number,
                    )
                else:
                    raise TransportError(
                        f"kernel driver owns interface {interface_number}; rerun with "
                        "--detach-kernel only if you understand the consequence"
                    )
        except NotImplementedError:
            pass

        try:
            self._util.claim_interface(dev, interface_number)
        except Exception as exc:
            if non_detaching_claim:
                raise TransportError(
                    f"cannot claim USB interface {interface_number}; non-detaching claim "
                    f"was attempted while its kernel driver remained active: {exc}"
                ) from exc
            raise TransportError(f"cannot claim USB interface {interface_number}: {exc}") from exc

        self._interface = selected
        self._ep_in = ep_in
        self._ep_out = ep_out
        LOG.info(
            "opened %04x:%04x interface=%d bulk_out=0x%02x bulk_in=0x%02x",
            dev.idVendor,
            dev.idProduct,
            interface_number,
            ep_out.bEndpointAddress,
            ep_in.bEndpointAddress,
        )

    def write(self, data: bytes) -> None:
        if self._ep_out is None:
            raise TransportError("USB transport is closed")
        view = memoryview(data)
        offset = 0
        while offset < len(view):
            chunk = view[offset : offset + self.write_chunk]
            try:
                written = int(self._ep_out.write(chunk, timeout=self.timeout_ms))
            except Exception as exc:
                raise TransportError(f"USB bulk OUT failed at offset {offset}: {exc}") from exc
            if written <= 0:
                raise TransportError("USB bulk OUT made no progress")
            offset += written

        # The factory app emits a zero-length packet if the full logical frame is an
        # exact multiple of the HS max packet size.  PyUSB/libusb normally handles
        # transfer termination, but explicitly attempt the ZLP for behavioral parity.
        max_packet = int(getattr(self._ep_out, "wMaxPacketSize", 512))
        if data and max_packet and len(data) % max_packet == 0:
            try:
                self._ep_out.write(b"", timeout=self.timeout_ms)
            except Exception as exc:
                LOG.debug("zero-length packet was not accepted: %s", exc)

    def write_single_transfer_no_zlp(self, data: bytes) -> None:
        """Issue exactly one bulk-OUT transfer and reject partial completion.

        This deliberately bypasses the ordinary logical-frame writer's short-write
        loop and max-packet-size ZLP behavior.  It exists for separately authorized,
        one-transfer diagnostic probes where a retry or follow-up ZLP would violate
        the experiment boundary.
        """

        if self._ep_out is None:
            raise TransportError("USB transport is closed")
        payload = bytes(data)
        if not payload:
            raise TransportError("single USB bulk OUT payload must not be empty")
        try:
            written = int(self._ep_out.write(payload, timeout=self.timeout_ms))
        except Exception as exc:
            raise TransportError(f"single USB bulk OUT failed: {exc}") from exc
        if written != len(payload):
            raise TransportError(
                f"short USB bulk OUT: wrote {written} of {len(payload)} bytes; "
                "refusing retry"
            )

    def read(self, *, timeout_ms: int = 500) -> bytes:
        if self._ep_in is None:
            raise TransportError("USB transport is closed")
        try:
            data = self._ep_in.read(16_384, timeout=timeout_ms)
            return bytes(data)
        except self._core.USBTimeoutError:
            return b""
        except Exception as exc:
            raise TransportError(f"USB bulk IN failed: {exc}") from exc

    def close(self) -> None:
        if self._dev is None:
            return
        dev = self._dev
        if self._interface is not None:
            interface_number = self._interface.bInterfaceNumber
            try:
                self._util.release_interface(dev, interface_number)
            except Exception:
                pass
        if self._detached_interface_number is not None:
            try:
                dev.attach_kernel_driver(self._detached_interface_number)
            except Exception:
                pass
        try:
            self._util.dispose_resources(dev)
        except Exception:
            pass
        self._dev = None
        self._interface = None
        self._ep_in = None
        self._ep_out = None
        self._detached_interface_number = None
