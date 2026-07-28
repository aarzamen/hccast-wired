"""Pure, platform-specific process plans for the Jetson live display service."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from hccast_wired.live.model import DesiredMode, LiveConfig


_ROOT_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
_SOURCE_PATH = "/usr/bin:/bin"
_LOCALE = "C.UTF-8"
_STOCK_GADGET_SERVICE = "nv-l4t-usb-device-mode.service"


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    """A process description that a later controller backend may execute."""

    name: str
    argv: tuple[str, ...]
    env: Mapping[str, str]
    run_as_user: str | None
    shell: Literal[False] = False


@dataclass(frozen=True, slots=True)
class ReconciliationPlan:
    """Commands required to return a stopped controller to its stock gadget state."""

    gadget_cleanup: ProcessSpec
    stock_service_start: ProcessSpec


@dataclass(frozen=True, slots=True)
class LiveCommandPlan:
    """All process descriptions required for one active display attempt."""

    xvfb: ProcessSpec
    openbox: ProcessSpec
    chromium: ProcessSpec | None
    x11vnc: ProcessSpec | None
    websockify: ProcessSpec | None
    encoder: ProcessSpec
    gadget: ProcessSpec
    gadget_cleanup: ProcessSpec
    stock_service_stop: ProcessSpec
    stock_service_start: ProcessSpec


def _absolute_path(value: str, name: str) -> str:
    if not Path(value).is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return value


def _environment(values: Mapping[str, str]) -> Mapping[str, str]:
    """Return an immutable complete replacement environment."""

    return MappingProxyType(dict(values))


class JetsonCommandBuilder:
    """Construct non-executing command plans for the documented Jetson deployment."""

    def __init__(
        self,
        *,
        hccast_executable: str = "/opt/hccast-live/.venv/bin/hccast-wired",
        xvfb_executable: str = "/usr/bin/Xvfb",
        openbox_executable: str = "/usr/bin/openbox",
        chromium_executable: str = "/usr/bin/chromium-browser",
        x11vnc_executable: str = "/usr/bin/x11vnc",
        websockify_executable: str = "/usr/bin/websockify",
        gstreamer_executable: str = "/usr/bin/gst-launch-1.0",
        gstreamer_inspect_executable: str = "/usr/bin/gst-inspect-1.0",
        systemctl_executable: str = "/usr/bin/systemctl",
        runuser_executable: str = "/usr/sbin/runuser",
        env_executable: str = "/usr/bin/env",
        find_executable: str = "/usr/bin/find",
        novnc_web_root: str = "/usr/share/novnc",
    ) -> None:
        self._hccast_executable = _absolute_path(hccast_executable, "hccast_executable")
        self._xvfb_executable = _absolute_path(xvfb_executable, "xvfb_executable")
        self._openbox_executable = _absolute_path(openbox_executable, "openbox_executable")
        self._chromium_executable = _absolute_path(chromium_executable, "chromium_executable")
        self._x11vnc_executable = _absolute_path(x11vnc_executable, "x11vnc_executable")
        self._websockify_executable = _absolute_path(websockify_executable, "websockify_executable")
        self._gstreamer_executable = _absolute_path(gstreamer_executable, "gstreamer_executable")
        self._gstreamer_inspect_executable = _absolute_path(
            gstreamer_inspect_executable, "gstreamer_inspect_executable"
        )
        self._systemctl_executable = _absolute_path(systemctl_executable, "systemctl_executable")
        self._runuser_executable = _absolute_path(runuser_executable, "runuser_executable")
        self._env_executable = _absolute_path(env_executable, "env_executable")
        self._find_executable = _absolute_path(find_executable, "find_executable")
        self._novnc_web_root = _absolute_path(novnc_web_root, "novnc_web_root")
        self._reconciliation_plan = self._create_reconciliation_plan()

    def build_reconciliation(self) -> ReconciliationPlan:
        """Describe stopped-mode cleanup and stock-gadget restoration without execution."""

        return self._reconciliation_plan

    def build(self, config: LiveConfig) -> LiveCommandPlan | None:
        """Describe an active mode without starting any process or changing the system."""

        if config.mode is DesiredMode.STOPPED:
            return None

        display = f":{config.display_number}"
        source_env = self._source_environment(config, display)
        root_env = self._root_environment()
        reconciliation = self.build_reconciliation()
        xvfb = ProcessSpec(
            name="xvfb",
            argv=(
                self._xvfb_executable,
                display,
                "-screen",
                "0",
                f"{config.width}x{config.height}x24",
                "-nolisten",
                "tcp",
                "-noreset",
            ),
            env=source_env,
            run_as_user=config.source_user,
        )
        openbox = ProcessSpec(
            name="openbox",
            argv=(self._openbox_executable,),
            env=source_env,
            run_as_user=config.source_user,
        )
        chromium = self._chromium_spec(config, source_env) if config.mode is DesiredMode.KIOSK else None
        x11vnc, websockify = self._preview_specs(config, display, source_env)
        encoder = ProcessSpec(
            name="encoder",
            argv=(
                self._gstreamer_executable,
                "-q",
                "ximagesrc",
                f"display-name={display}",
                "use-damage=false",
                "show-pointer=true",
                "!",
                "videoconvert",
                "!",
                (
                    "video/x-raw,format=I420,"
                    f"width={config.width},height={config.height},framerate={config.fps}/1"
                ),
                "!",
                "x264enc",
                f"bitrate={config.bitrate_kbps}",
                "tune=zerolatency",
                "byte-stream=true",
                "aud=true",
                f"key-int-max={config.fps * 2}",
                "!",
                "h264parse",
                "config-interval=-1",
                "!",
                "video/x-h264,profile=baseline,stream-format=byte-stream,alignment=au",
                "!",
                "fdsink",
                "fd=1",
            ),
            env=source_env,
            run_as_user=config.source_user,
        )
        gadget = ProcessSpec(
            name="gadget-stream",
            argv=(
                self._hccast_executable,
                "-vv",
                "gadget-stream",
                "--aoa-mode",
                "direct",
                "--enable-timeout",
                "120",
                "--orientation",
                "portrait",
                "--width",
                str(config.width),
                "--height",
                str(config.height),
                "--source-width",
                str(config.width),
                "--source-height",
                str(config.height),
                "--fps",
                str(config.fps),
                "--packetization",
                "access-unit",
                "-",
            ),
            env=_environment({**root_env, "PYTHONUNBUFFERED": "1"}),
            run_as_user=None,
        )
        stock_service_stop = ProcessSpec(
            name="stock-gadget-stop",
            argv=(self._systemctl_executable, "stop", _STOCK_GADGET_SERVICE),
            env=root_env,
            run_as_user=None,
        )
        return LiveCommandPlan(
            xvfb=xvfb,
            openbox=openbox,
            chromium=chromium,
            x11vnc=x11vnc,
            websockify=websockify,
            encoder=encoder,
            gadget=gadget,
            gadget_cleanup=reconciliation.gadget_cleanup,
            stock_service_stop=stock_service_stop,
            stock_service_start=reconciliation.stock_service_start,
        )

    def capability_commands(self) -> tuple[ProcessSpec, ...]:
        """Describe only read-only deployment probes; this method never executes them."""

        root_env = self._root_environment()
        return (
            self._probe("probe-hccast-wired", (self._hccast_executable, "--help"), root_env),
            self._probe("probe-xvfb", (self._xvfb_executable, "--version"), root_env),
            self._probe("probe-openbox", (self._openbox_executable, "--version"), root_env),
            self._probe("probe-chromium", (self._chromium_executable, "--version"), root_env),
            self._probe("probe-x11vnc", (self._x11vnc_executable, "-version"), root_env),
            self._probe("probe-websockify", (self._websockify_executable, "--version"), root_env),
            self._probe("probe-runuser", (self._runuser_executable, "--version"), root_env),
            self._probe(
                "probe-gstreamer-element-ximagesrc",
                (self._gstreamer_inspect_executable, "--exists", "ximagesrc"),
                root_env,
            ),
            self._probe(
                "probe-gstreamer-element-videoconvert",
                (self._gstreamer_inspect_executable, "--exists", "videoconvert"),
                root_env,
            ),
            self._probe(
                "probe-gstreamer-element-x264enc",
                (self._gstreamer_inspect_executable, "--exists", "x264enc"),
                root_env,
            ),
            self._probe(
                "probe-gstreamer-element-h264parse",
                (self._gstreamer_inspect_executable, "--exists", "h264parse"),
                root_env,
            ),
            self._probe(
                "probe-gstreamer-element-fdsink",
                (self._gstreamer_inspect_executable, "--exists", "fdsink"),
                root_env,
            ),
            self._probe(
                "probe-nvidia-usb-gadget-service",
                (self._systemctl_executable, "is-active", "--quiet", _STOCK_GADGET_SERVICE),
                root_env,
            ),
            self._probe(
                "probe-udc-entries",
                (
                    self._find_executable,
                    "/sys/class/udc",
                    "-mindepth",
                    "1",
                    "-maxdepth",
                    "1",
                    "-printf",
                    "%f\\n",
                ),
                root_env,
            ),
        )

    def _chromium_spec(self, config: LiveConfig, source_env: Mapping[str, str]) -> ProcessSpec:
        return ProcessSpec(
            name="chromium",
            argv=(
                self._chromium_executable,
                "--no-first-run",
                "--no-default-browser-check",
                "--kiosk",
                f"--display=:{config.display_number}",
                config.kiosk_url,
            ),
            env=source_env,
            run_as_user=config.source_user,
        )

    def _preview_specs(
        self, config: LiveConfig, display: str, source_env: Mapping[str, str]
    ) -> tuple[ProcessSpec | None, ProcessSpec | None]:
        if not config.novnc_enabled:
            return None, None
        x11vnc = ProcessSpec(
            name="x11vnc",
            argv=(
                self._x11vnc_executable,
                "-display",
                display,
                "-forever",
                "-shared",
                "-rfbport",
                "5900",
                "-listen",
                config.novnc_host,
                "-localhost",
            ),
            env=source_env,
            run_as_user=config.source_user,
        )
        websockify = ProcessSpec(
            name="websockify",
            argv=(
                self._websockify_executable,
                f"--web={self._novnc_web_root}",
                f"{config.novnc_host}:{config.novnc_port}",
                f"{config.novnc_host}:5900",
            ),
            env=source_env,
            run_as_user=config.source_user,
        )
        return x11vnc, websockify

    def _source_environment(self, config: LiveConfig, display: str) -> Mapping[str, str]:
        return _environment(
            {
                "HOME": f"/home/{config.source_user}",
                "USER": config.source_user,
                "LOGNAME": config.source_user,
                "PATH": _SOURCE_PATH,
                "LANG": _LOCALE,
                "LC_ALL": _LOCALE,
                "DISPLAY": display,
            }
        )

    def _root_environment(self) -> Mapping[str, str]:
        return _environment(
            {
                "HOME": "/root",
                "USER": "root",
                "LOGNAME": "root",
                "PATH": _ROOT_PATH,
                "LANG": _LOCALE,
                "LC_ALL": _LOCALE,
            }
        )

    def _create_reconciliation_plan(self) -> ReconciliationPlan:
        root_env = self._root_environment()
        return ReconciliationPlan(
            gadget_cleanup=ProcessSpec(
                name="gadget-cleanup",
                argv=(self._hccast_executable, "-vv", "gadget-stop"),
                env=root_env,
                run_as_user=None,
            ),
            stock_service_start=ProcessSpec(
                name="stock-gadget-start",
                argv=(self._systemctl_executable, "start", _STOCK_GADGET_SERVICE),
                env=root_env,
                run_as_user=None,
            ),
        )

    @staticmethod
    def _probe(name: str, argv: tuple[str, ...], env: Mapping[str, str]) -> ProcessSpec:
        return ProcessSpec(name=name, argv=argv, env=env, run_as_user=None)
