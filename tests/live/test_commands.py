from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Iterable, cast

import pytest

from hccast_wired.live.commands import (
    JetsonCommandBuilder,
    LiveCommandPlan,
    ProcessSpec,
    ReconciliationPlan,
)
from hccast_wired.live.model import DesiredMode, LiveConfig


def _all_specs(plan: LiveCommandPlan) -> Iterable[ProcessSpec]:
    for spec in (
        plan.xvfb,
        plan.openbox,
        plan.chromium,
        plan.x11vnc,
        plan.websockify,
        plan.encoder,
        plan.gadget,
        plan.gadget_cleanup,
        plan.stock_service_stop,
        plan.stock_service_start,
    ):
        if spec is not None:
            yield spec


def test_desktop_plan_uses_documented_portrait_size() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert plan.xvfb.argv == (
        "/usr/bin/Xvfb",
        ":99",
        "-screen",
        "0",
        "640x1136x24",
        "-nolisten",
        "tcp",
        "-noreset",
    )
    assert plan.chromium is None


def test_openbox_inherits_display_from_its_complete_environment() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert plan.openbox.argv == ("/usr/bin/openbox",)
    assert plan.openbox.env["DISPLAY"] == ":99"


def test_kiosk_plan_passes_url_as_one_argument_without_shell() -> None:
    config = LiveConfig(mode=DesiredMode.KIOSK, kiosk_url="http://127.0.0.1:3000/a?x=1&y=2")
    plan = JetsonCommandBuilder().build(config)

    assert plan is not None and plan.chromium is not None
    assert plan.chromium.argv[-1] == config.kiosk_url
    assert plan.chromium.shell is False
    assert config.kiosk_url not in plan.chromium.env.values()


def test_encoder_is_baseline_annexb_access_unit_stream() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    joined = " ".join(plan.encoder.argv)
    for required in (
        "video/x-raw,format=I420,width=640,height=1136,framerate=10/1",
        "x264enc",
        "bitrate=4000",
        "tune=zerolatency",
        "byte-stream=true",
        "aud=true",
        "key-int-max=20",
        "profile=baseline",
        "alignment=au",
        "fdsink",
    ):
        assert required in joined


def test_gadget_stream_uses_verified_direct_mode_and_documented_size() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert "--aoa-mode" in plan.gadget.argv
    assert plan.gadget.argv[plan.gadget.argv.index("--aoa-mode") + 1] == "direct"
    assert plan.gadget.argv[plan.gadget.argv.index("--enable-timeout") + 1] == "120"
    for flag, value in (("--width", "640"), ("--height", "1136"), ("--fps", "10")):
        assert plan.gadget.argv[plan.gadget.argv.index(flag) + 1] == value
    assert plan.gadget.argv[plan.gadget.argv.index("--packetization") + 1] == "access-unit"
    assert "--send-settings" not in plan.gadget.argv
    assert plan.gadget.argv[-1] == "-"
    assert plan.gadget.env["PYTHONUNBUFFERED"] == "1"


def test_preview_listeners_are_loopback_only() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None and plan.x11vnc is not None and plan.websockify is not None
    listen_index = plan.x11vnc.argv.index("-listen")
    assert plan.x11vnc.argv[listen_index : listen_index + 2] == ("-listen", "127.0.0.1")
    assert "-localhost" in plan.x11vnc.argv
    assert "127.0.0.1:6080" in plan.websockify.argv
    assert "127.0.0.1:5900" in plan.websockify.argv
    assert "--web=/usr/share/novnc" in plan.websockify.argv
    assert "0.0.0.0" not in plan.x11vnc.argv + plan.websockify.argv


def test_preview_specs_are_absent_when_preview_is_disabled() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP, novnc_enabled=False))

    assert plan is not None
    assert plan.x11vnc is None
    assert plan.websockify is None


def test_production_gadget_executable_is_absolute() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert plan.gadget.argv[0] == "/opt/hccast-live/.venv/bin/hccast-wired"
    assert plan.gadget.env["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"


def test_specs_assign_privileges_and_never_enable_a_shell() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.KIOSK))

    assert plan is not None
    source_specs = (plan.xvfb, plan.openbox, plan.chromium, plan.x11vnc, plan.websockify, plan.encoder)
    assert all(spec is None or spec.run_as_user == "hccast" for spec in source_specs)
    assert plan.gadget.run_as_user is None
    assert plan.gadget_cleanup.run_as_user is None
    assert plan.stock_service_stop.run_as_user is None
    assert plan.stock_service_start.run_as_user is None
    assert all(spec.shell is False for spec in _all_specs(plan))


def test_stopped_mode_needs_no_processes() -> None:
    assert JetsonCommandBuilder().build(LiveConfig()) is None


def test_stopped_mode_has_an_independent_reconciliation_plan() -> None:
    builder = JetsonCommandBuilder()

    assert builder.build(LiveConfig(mode=DesiredMode.STOPPED)) is None
    reconciliation = builder.build_reconciliation()

    assert isinstance(reconciliation, ReconciliationPlan)
    assert reconciliation.gadget_cleanup.name == "gadget-cleanup"
    assert reconciliation.gadget_cleanup.argv == (
        "/opt/hccast-live/.venv/bin/hccast-wired",
        "-vv",
        "gadget-stop",
    )
    assert reconciliation.gadget_cleanup.env == {
        "HOME": "/root",
        "USER": "root",
        "LOGNAME": "root",
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    assert reconciliation.gadget_cleanup.run_as_user is None
    assert reconciliation.gadget_cleanup.shell is False
    assert reconciliation.stock_service_start.name == "stock-gadget-start"
    assert reconciliation.stock_service_start.argv == (
        "/usr/bin/systemctl",
        "start",
        "nv-l4t-usb-device-mode.service",
    )
    assert reconciliation.stock_service_start.env == reconciliation.gadget_cleanup.env
    assert reconciliation.stock_service_start.run_as_user is None
    assert reconciliation.stock_service_start.shell is False


def test_reconciliation_plan_is_immutable() -> None:
    reconciliation = JetsonCommandBuilder().build_reconciliation()

    with pytest.raises(FrozenInstanceError):
        reconciliation.gadget_cleanup = reconciliation.stock_service_start  # type: ignore[misc]
    with pytest.raises(TypeError):
        cast(dict[str, str], reconciliation.gadget_cleanup.env)["PATH"] = "/other"


def test_active_plan_reuses_its_standalone_reconciliation_specs() -> None:
    builder = JetsonCommandBuilder()
    reconciliation = builder.build_reconciliation()
    plan = builder.build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert plan.gadget_cleanup is reconciliation.gadget_cleanup
    assert plan.stock_service_start is reconciliation.stock_service_start


def test_reconciliation_propagates_injected_executable_paths(tmp_path: Path) -> None:
    hccast_executable = str(tmp_path / "hccast-wired")
    systemctl_executable = str(tmp_path / "systemctl")
    builder = JetsonCommandBuilder(
        hccast_executable=hccast_executable,
        systemctl_executable=systemctl_executable,
    )

    reconciliation = builder.build_reconciliation()

    assert reconciliation.gadget_cleanup.argv[0] == hccast_executable
    assert reconciliation.stock_service_start.argv[0] == systemctl_executable


def test_specs_are_immutable_and_environments_are_not_controller_overlays() -> None:
    plan = JetsonCommandBuilder().build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    with pytest.raises(FrozenInstanceError):
        plan.xvfb.name = "other"  # type: ignore[misc]
    for spec in _all_specs(plan):
        assert {"HOME", "USER", "LOGNAME", "PATH", "LANG", "LC_ALL"} <= set(spec.env)


def test_constructor_accepts_absolute_fake_binary_paths(tmp_path: Path) -> None:
    paths = {name: str(tmp_path / name) for name in (
        "hccast-wired",
        "Xvfb",
        "openbox",
        "chromium",
        "x11vnc",
        "websockify",
        "gst-launch-1.0",
        "gst-inspect-1.0",
        "systemctl",
        "runuser",
        "env",
        "find",
    )}
    builder = JetsonCommandBuilder(
        hccast_executable=paths["hccast-wired"],
        xvfb_executable=paths["Xvfb"],
        openbox_executable=paths["openbox"],
        chromium_executable=paths["chromium"],
        x11vnc_executable=paths["x11vnc"],
        websockify_executable=paths["websockify"],
        gstreamer_executable=paths["gst-launch-1.0"],
        gstreamer_inspect_executable=paths["gst-inspect-1.0"],
        systemctl_executable=paths["systemctl"],
        runuser_executable=paths["runuser"],
        env_executable=paths["env"],
        find_executable=paths["find"],
        novnc_web_root=str(tmp_path / "novnc"),
    )

    plan = builder.build(LiveConfig(mode=DesiredMode.DESKTOP))

    assert plan is not None
    assert plan.xvfb.argv[0] == paths["Xvfb"]
    assert plan.gadget.argv[0] == paths["hccast-wired"]
    assert plan.stock_service_stop.argv[0] == paths["systemctl"]
    probe_by_name = {probe.name: probe for probe in builder.capability_commands()}
    assert probe_by_name["probe-gstreamer-element-ximagesrc"].argv[0] == paths["gst-inspect-1.0"]
    assert probe_by_name["probe-udc-entries"].argv[0] == paths["find"]


@pytest.mark.parametrize("path", ["relative/program", "program"])
def test_constructor_rejects_relative_executable_paths(path: str) -> None:
    with pytest.raises(ValueError, match="absolute"):
        JetsonCommandBuilder(xvfb_executable=path)


def test_capability_commands_are_read_only_descriptions() -> None:
    probes = JetsonCommandBuilder().capability_commands()

    assert {probe.name for probe in probes} >= {
        "probe-hccast-wired",
        "probe-xvfb",
        "probe-openbox",
        "probe-chromium",
        "probe-x11vnc",
        "probe-websockify",
        "probe-runuser",
        "probe-nvidia-usb-gadget-service",
        "probe-udc-entries",
    }
    probe_by_name = {probe.name: probe for probe in probes}
    for element in ("ximagesrc", "videoconvert", "x264enc", "h264parse", "fdsink"):
        assert probe_by_name[f"probe-gstreamer-element-{element}"].argv == (
            "/usr/bin/gst-inspect-1.0",
            "--exists",
            element,
        )
    assert probe_by_name["probe-udc-entries"].argv == (
        "/usr/bin/find",
        "/sys/class/udc",
        "-mindepth",
        "1",
        "-maxdepth",
        "1",
        "-printf",
        "%f\\n",
    )
    assert any(probe.argv[-1] == "nv-l4t-usb-device-mode.service" for probe in probes)
    assert all(probe.shell is False and probe.run_as_user is None for probe in probes)
    assert all(
        forbidden not in argument
        for probe in probes
        for argument in probe.argv
        for forbidden in ("start", "stop", "restart", "install", "enable", "gadget-stream", "gadget-stop")
    )
