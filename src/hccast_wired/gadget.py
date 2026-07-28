"""ConfigFS lifecycle for the HCCAST FunctionFS gadget."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from .functionfs import FunctionFSError

LOG = logging.getLogger(__name__)

AOA_VENDOR_ID = 0x18D1
AOA_ACCESSORY_PID = 0x2D00
DEFAULT_PRE_AOA_VENDOR_ID = 0x1209
DEFAULT_PRE_AOA_PRODUCT_ID = 0x0001


class GadgetError(FunctionFSError):
    pass


def _run(command: list[str]) -> None:
    LOG.debug("running: %s", " ".join(command))
    try:
        subprocess.run(command, check=True)
    except FileNotFoundError as exc:
        raise GadgetError(f"required command not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise GadgetError(f"command failed ({exc.returncode}): {' '.join(command)}") from exc


def _is_mounted(path: Path, fs_type: str | None = None) -> bool:
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    resolved = str(path.resolve())
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[1] == resolved:
            return fs_type is None or parts[2] == fs_type
    return False


def _write(path: Path, value: str) -> None:
    try:
        path.write_text(value, encoding="ascii")
    except OSError as exc:
        raise GadgetError(f"cannot write {path}: {exc}") from exc


def _remove_if_exists(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    except FileNotFoundError:
        pass


class ConfigFSGadget:
    """Create one vendor-specific FunctionFS gadget and bind it to a UDC."""

    def __init__(
        self,
        *,
        name: str = "hccast",
        function_name: str = "hccast",
        mountpoint: str | Path = "/dev/ffs-hccast",
        udc: str | None = None,
        configfs_root: str | Path = "/sys/kernel/config/usb_gadget",
    ) -> None:
        self.name = name
        self.function_name = function_name
        self.mountpoint = Path(mountpoint)
        self.udc_requested = udc
        self.configfs_root = Path(configfs_root)
        self.root = self.configfs_root / name
        self.config = self.root / "configs" / "c.1"
        self.function = self.root / "functions" / f"ffs.{function_name}"
        self.link = self.config / f"ffs.{function_name}"
        self._mounted_ffs = False
        self._bound = False
        self._udc_name: str | None = None

    def require_root(self) -> None:
        if os.geteuid() != 0:
            raise GadgetError(
                "USB gadget setup requires root. Run the installed interpreter with sudo, "
                "for example: sudo .venv/bin/hccast-wired gadget-stream ..."
            )

    def ensure_configfs(self) -> None:
        self.require_root()
        if not Path("/sys/kernel/config").exists():
            Path("/sys/kernel/config").mkdir(parents=True, exist_ok=True)
        if not _is_mounted(Path("/sys/kernel/config"), "configfs"):
            _run(["modprobe", "libcomposite"])
            _run(["mount", "-t", "configfs", "none", "/sys/kernel/config"])
        if not self.configfs_root.exists():
            _run(["modprobe", "libcomposite"])
        if not self.configfs_root.exists():
            raise GadgetError(
                f"USB gadget configfs is unavailable at {self.configfs_root}; "
                "kernel needs CONFIG_USB_GADGET and CONFIG_USB_CONFIGFS"
            )

    def available_udcs(self) -> list[str]:
        udc_root = Path("/sys/class/udc")
        if not udc_root.exists():
            return []
        return sorted(path.name for path in udc_root.iterdir())

    def _udc_owner(self, udc_name: str) -> Path | None:
        if not self.configfs_root.exists():
            return None
        for udc_file in self.configfs_root.glob("*/UDC"):
            try:
                value = udc_file.read_text(encoding="ascii").strip()
            except OSError:
                continue
            if value == udc_name and udc_file.parent != self.root:
                return udc_file.parent
        return None

    def choose_udc(self) -> str:
        available = self.available_udcs()
        if self.udc_requested:
            if self.udc_requested not in available:
                raise GadgetError(
                    f"requested UDC {self.udc_requested!r} not found; available={available}"
                )
            selected = self.udc_requested
        elif len(available) == 1:
            selected = available[0]
        elif not available:
            raise GadgetError(
                "no USB Device Controller is exposed under /sys/class/udc; "
                "wrong port, kernel, device tree, or hardware"
            )
        else:
            raise GadgetError(f"multiple UDCs found; choose one with --udc: {available}")

        owner = self._udc_owner(selected)
        if owner is not None:
            raise GadgetError(
                f"UDC {selected!r} is already bound by gadget {owner}. "
                "Stop the board's existing USB gadget/device-mode service or unbind "
                "that gadget before running HCCAST."
            )
        return selected

    def create(
        self,
        *,
        vendor_id: int,
        product_id: int,
        manufacturer: str,
        product: str,
        serial: str,
    ) -> None:
        self.ensure_configfs()
        if self.root.exists():
            raise GadgetError(
                f"gadget {self.root} already exists. Run gadget-stop or remove stale state first."
            )

        self.root.mkdir()
        _write(self.root / "idVendor", f"0x{vendor_id:04x}")
        _write(self.root / "idProduct", f"0x{product_id:04x}")
        _write(self.root / "bcdUSB", "0x0200")
        _write(self.root / "bcdDevice", "0x0100")
        _write(self.root / "bDeviceClass", "0x00")
        _write(self.root / "bDeviceSubClass", "0x00")
        _write(self.root / "bDeviceProtocol", "0x00")

        device_strings = self.root / "strings" / "0x409"
        device_strings.mkdir(parents=True)
        _write(device_strings / "manufacturer", manufacturer)
        _write(device_strings / "product", product)
        _write(device_strings / "serialnumber", serial)

        config_strings = self.config / "strings" / "0x409"
        config_strings.mkdir(parents=True)
        _write(config_strings / "configuration", "AOA / HCCAST wired display")
        _write(self.config / "MaxPower", "250")
        _write(self.config / "bmAttributes", "0x80")

        self.function.mkdir(parents=True)
        LOG.info(
            "created gadget %s as %04x:%04x",
            self.root,
            vendor_id,
            product_id,
        )

    def mount_functionfs(self) -> None:
        self.mountpoint.mkdir(parents=True, exist_ok=True)
        if _is_mounted(self.mountpoint, "functionfs"):
            return
        _run(["mount", "-t", "functionfs", self.function_name, str(self.mountpoint)])
        self._mounted_ffs = True
        LOG.info("mounted FunctionFS %s at %s", self.function_name, self.mountpoint)

    def link_function(self) -> None:
        if self.link.exists() or self.link.is_symlink():
            return
        try:
            self.link.symlink_to(self.function)
        except OSError as exc:
            raise GadgetError(f"cannot link FunctionFS function into config: {exc}") from exc

    def bind(self) -> None:
        if self._bound:
            return
        self._udc_name = self.choose_udc()
        _write(self.root / "UDC", self._udc_name)
        self._bound = True
        LOG.info("bound gadget to UDC %s", self._udc_name)

    def unbind(self) -> None:
        if not self.root.exists():
            return
        try:
            _write(self.root / "UDC", "")
        except GadgetError as exc:
            LOG.debug("UDC unbind ignored: %s", exc)
        self._bound = False

    def set_identity(
        self,
        *,
        vendor_id: int,
        product_id: int,
        manufacturer: str | None = None,
        product: str | None = None,
    ) -> None:
        if self._bound:
            raise GadgetError("unbind gadget before changing USB identity")
        _write(self.root / "idVendor", f"0x{vendor_id:04x}")
        _write(self.root / "idProduct", f"0x{product_id:04x}")
        strings = self.root / "strings" / "0x409"
        if manufacturer is not None:
            _write(strings / "manufacturer", manufacturer)
        if product is not None:
            _write(strings / "product", product)

    def reenumerate_as_aoa(self, *, disconnect_s: float = 0.35) -> None:
        self.unbind()
        time.sleep(disconnect_s)
        self.set_identity(
            vendor_id=AOA_VENDOR_ID,
            product_id=AOA_ACCESSORY_PID,
            manufacturer="Android",
            product="Android Accessory",
        )
        self.bind()
        LOG.info("re-enumerated as Android Open Accessory 18d1:2d00")

    def cleanup(self) -> None:
        self.unbind()
        time.sleep(0.05)
        _remove_if_exists(self.link)

        # FunctionFS must be unmounted after all ep files are closed.
        if _is_mounted(self.mountpoint, "functionfs"):
            try:
                _run(["umount", str(self.mountpoint)])
            except GadgetError as exc:
                LOG.warning("FunctionFS unmount failed: %s", exc)
        try:
            self.mountpoint.rmdir()
        except OSError:
            pass

        # Remove configfs nodes in leaf-to-root order.
        paths = [
            self.function,
            self.config / "strings" / "0x409",
            self.config,
            self.root / "configs",
            self.root / "functions",
            self.root / "strings" / "0x409",
            self.root / "strings",
            self.root,
        ]
        for path in paths:
            try:
                path.rmdir()
            except FileNotFoundError:
                pass
            except OSError as exc:
                LOG.debug("could not remove %s: %s", path, exc)
        LOG.info("gadget cleanup complete")

    @classmethod
    def force_cleanup(
        cls,
        *,
        name: str = "hccast",
        function_name: str = "hccast",
        mountpoint: str | Path = "/dev/ffs-hccast",
    ) -> None:
        gadget = cls(name=name, function_name=function_name, mountpoint=mountpoint)
        gadget.cleanup()
