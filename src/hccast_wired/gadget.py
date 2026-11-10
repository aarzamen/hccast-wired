"""ConfigFS lifecycle for the HCCAST FunctionFS gadget."""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .functionfs import FunctionFSError

LOG = logging.getLogger(__name__)

AOA_VENDOR_ID = 0x18D1
AOA_ACCESSORY_PID = 0x2D00
DEFAULT_PRE_AOA_VENDOR_ID = 0x1209
DEFAULT_PRE_AOA_PRODUCT_ID = 0x0001


class GadgetError(FunctionFSError):
    pass


@dataclass(frozen=True, slots=True)
class CleanupFailure:
    """One cleanup action that did not complete."""

    action: str
    message: str


@dataclass(frozen=True, slots=True)
class CleanupReport:
    """What a cleanup attempted, what failed and which postconditions were checked.

    ``success`` is true only when no action failed and every expected
    postcondition was verified. Callers that exit on this report can therefore
    not mistake a half-removed gadget, a lingering FunctionFS mount or a still
    bound UDC for a clean stop.
    """

    attempted_actions: tuple[str, ...]
    errors: tuple[CleanupFailure, ...]
    verified_postconditions: tuple[str, ...]
    failed_postconditions: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.errors and not self.failed_postconditions

    def to_mapping(self) -> dict[str, object]:
        return {
            "attempted_actions": list(self.attempted_actions),
            "errors": [{"action": e.action, "message": e.message} for e in self.errors],
            "verified_postconditions": list(self.verified_postconditions),
            "failed_postconditions": list(self.failed_postconditions),
            "success": self.success,
        }


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


def _rmdir(path: Path) -> None:
    """Remove one configfs node; its attribute files disappear with it."""

    path.rmdir()


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
        # Ownership of every resource this invocation creates. cleanup() releases
        # only what is recorded here, so a rejected start can never unbind or
        # remove a gadget that belongs to another session. Stale state is removed
        # explicitly through force_cleanup() instead.
        self._created_root = False
        self._created_function = False
        self._created_mountpoint = False
        self._mounted_ffs = False
        self._linked = False
        self._bound = False
        self._udc_name: str | None = None

    @property
    def owned_resources(self) -> dict[str, bool]:
        """Report which resources this invocation currently owns."""

        return {
            "root": self._created_root,
            "function": self._created_function,
            "mountpoint": self._created_mountpoint,
            "functionfs_mount": self._mounted_ffs,
            "link": self._linked,
            "udc_binding": self._bound,
        }

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
        self._created_root = True
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
        self._created_function = True
        LOG.info(
            "created gadget %s as %04x:%04x",
            self.root,
            vendor_id,
            product_id,
        )

    def mount_functionfs(self) -> None:
        if not self.mountpoint.exists():
            self.mountpoint.mkdir(parents=True)
            self._created_mountpoint = True
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
        self._linked = True

    def bind(self) -> None:
        if self._bound:
            return
        self._udc_name = self.choose_udc()
        _write(self.root / "UDC", self._udc_name)
        self._bound = True
        LOG.info("bound gadget to UDC %s", self._udc_name)

    def unbind(self) -> None:
        """Release a UDC binding made by this invocation; foreign bindings are untouched."""

        if not self._bound:
            return
        if self.root.exists():
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

    def _configfs_nodes(self) -> list[Path]:
        """ConfigFS nodes of this gadget in leaf-to-root removal order."""

        return [
            self.config / "strings" / "0x409",
            self.config / "strings",
            self.config,
            self.root / "configs",
            self.root / "functions",
            self.root / "strings" / "0x409",
            self.root / "strings",
            self.root,
        ]

    def _udc_released(self) -> bool:
        """True when this gadget no longer claims any UDC."""

        if not self.root.exists():
            return True
        try:
            return (self.root / "UDC").read_text(encoding="ascii").strip() == ""
        except OSError:
            return False

    def _verify(
        self,
        checks: list[tuple[str, bool]],
        verified: list[str],
        failed: list[str],
    ) -> None:
        for name, ok in checks:
            (verified if ok else failed).append(name)

    def cleanup(self) -> CleanupReport:
        """Release only the resources this invocation created or bound.

        A gadget, mount, link or UDC binding that already existed before this
        invocation is left exactly as it was. Removing such stale or foreign
        state is a separate explicit operation: ``remove_stale_state``.

        Nothing is suppressed: every failed action is recorded in the returned
        report, and the postconditions of every owned resource are verified
        afterwards, so the caller can tell a clean stop from a partial one.
        """

        attempted: list[str] = []
        errors: list[CleanupFailure] = []
        owned_binding = self._bound
        owned_mount = self._mounted_ffs
        owned_mountpoint = self._created_mountpoint
        owned_function = self._created_function
        owned_root = self._created_root

        if self._bound:
            attempted.append("udc-unbind")
            try:
                _write(self.root / "UDC", "")
            except GadgetError as exc:
                if self.root.exists():
                    errors.append(CleanupFailure("udc-unbind", str(exc)))
            self._bound = False
            time.sleep(0.05)
        if self._linked:
            attempted.append("link-remove")
            try:
                _remove_if_exists(self.link)
                self._linked = False
            except OSError as exc:
                errors.append(CleanupFailure("link-remove", str(exc)))

        # FunctionFS must be unmounted after all ep files are closed.
        if self._mounted_ffs:
            if _is_mounted(self.mountpoint, "functionfs"):
                attempted.append("functionfs-unmount")
                try:
                    _run(["umount", str(self.mountpoint)])
                    self._mounted_ffs = False
                except GadgetError as exc:
                    errors.append(CleanupFailure("functionfs-unmount", str(exc)))
            else:
                self._mounted_ffs = False
        if self._created_mountpoint and not self._mounted_ffs:
            # A directory that is still a mount point cannot be removed; the
            # failed unmount above is already recorded and the postcondition
            # check below reports the lingering directory.
            attempted.append("mountpoint-remove")
            try:
                self.mountpoint.rmdir()
                self._created_mountpoint = False
            except FileNotFoundError:
                self._created_mountpoint = False
            except OSError as exc:
                errors.append(CleanupFailure("mountpoint-remove", str(exc)))

        if self._created_function:
            attempted.append("function-remove")
            try:
                _rmdir(self.function)
                self._created_function = False
            except FileNotFoundError:
                self._created_function = False
            except OSError as exc:
                errors.append(CleanupFailure("function-remove", str(exc)))
        if self._created_root:
            attempted.append("configfs-remove")
            for path in self._configfs_nodes():
                try:
                    _rmdir(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    # Built-in groups may refuse removal on real configfs; only
                    # the gadget root itself must be gone, verified below.
                    LOG.debug("could not remove %s: %s", path, exc)
            if self.root.exists():
                errors.append(CleanupFailure("configfs-remove", f"{self.root} still exists"))
            else:
                self._created_root = False

        verified: list[str] = []
        failed: list[str] = []
        checks: list[tuple[str, bool]] = []
        if owned_binding:
            checks.append(("udc-released", self._udc_released()))
        if owned_mount:
            checks.append(("functionfs-unmounted", not _is_mounted(self.mountpoint, "functionfs")))
        if owned_mountpoint:
            checks.append(("mountpoint-removed", not self.mountpoint.exists()))
        if owned_function:
            checks.append(("function-removed", not self.function.exists()))
        if owned_root:
            checks.append(("gadget-removed", not self.root.exists()))
        self._verify(checks, verified, failed)
        report = CleanupReport(tuple(attempted), tuple(errors), tuple(verified), tuple(failed))
        if report.success:
            LOG.info("gadget cleanup complete")
        else:
            LOG.warning("gadget cleanup incomplete: %s", report.to_mapping())
        return report

    def remove_stale_state(self) -> CleanupReport:
        """Explicitly unbind and remove this gadget's state regardless of ownership.

        This is the ``gadget-stop`` operation. It is never run implicitly by a
        failed start; an operator asks for it after confirming no other session
        still needs the gadget. The returned report verifies that the UDC is
        released, FunctionFS is unmounted and the gadget is gone.
        """

        attempted: list[str] = []
        errors: list[CleanupFailure] = []
        if self.root.exists():
            attempted.append("udc-unbind")
            try:
                _write(self.root / "UDC", "")
            except GadgetError as exc:
                errors.append(CleanupFailure("udc-unbind", str(exc)))
        self._bound = False
        time.sleep(0.05)
        attempted.append("link-remove")
        try:
            _remove_if_exists(self.link)
        except OSError as exc:
            errors.append(CleanupFailure("link-remove", str(exc)))
        self._linked = False
        if _is_mounted(self.mountpoint, "functionfs"):
            attempted.append("functionfs-unmount")
            try:
                _run(["umount", str(self.mountpoint)])
            except GadgetError as exc:
                errors.append(CleanupFailure("functionfs-unmount", str(exc)))
        self._mounted_ffs = False
        if self.mountpoint.exists() and not _is_mounted(self.mountpoint, "functionfs"):
            attempted.append("mountpoint-remove")
            try:
                self.mountpoint.rmdir()
            except OSError as exc:
                errors.append(CleanupFailure("mountpoint-remove", str(exc)))
        self._created_mountpoint = False
        if self.root.exists():
            attempted.append("configfs-remove")
            for path in [self.function, *self._configfs_nodes()]:
                try:
                    _rmdir(path)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    LOG.debug("could not remove %s: %s", path, exc)
            if self.root.exists():
                errors.append(CleanupFailure("configfs-remove", f"{self.root} still exists"))
        self._created_function = self._created_root = False

        verified: list[str] = []
        failed: list[str] = []
        self._verify(
            [
                ("udc-released", self._udc_released()),
                ("functionfs-unmounted", not _is_mounted(self.mountpoint, "functionfs")),
                ("mountpoint-removed", not self.mountpoint.exists()),
                ("function-removed", not self.function.exists()),
                ("gadget-removed", not self.root.exists()),
            ],
            verified,
            failed,
        )
        report = CleanupReport(tuple(attempted), tuple(errors), tuple(verified), tuple(failed))
        if report.success:
            LOG.info("stale gadget state removal complete")
        else:
            LOG.warning("stale gadget state removal incomplete: %s", report.to_mapping())
        return report

    @classmethod
    def force_cleanup(
        cls,
        *,
        name: str = "hccast",
        function_name: str = "hccast",
        mountpoint: str | Path = "/dev/ffs-hccast",
        configfs_root: str | Path = "/sys/kernel/config/usb_gadget",
    ) -> CleanupReport:
        gadget = cls(
            name=name,
            function_name=function_name,
            mountpoint=mountpoint,
            configfs_root=configfs_root,
        )
        return gadget.remove_stale_state()
