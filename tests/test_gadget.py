"""Ownership rules for the ConfigFS gadget, exercised against a fake configfs tree.

No test here touches /sys, a UDC, mount(8) or root privileges. The fake replaces
the module's command runner and mount probe so the gadget's own bookkeeping is
what is under test.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hccast_wired import cli
from hccast_wired import gadget as gadget_module
from hccast_wired.gadget import ConfigFSGadget, GadgetError


class FakeSystem:
    """Records mounts and commands; stands in for mount(8), umount(8) and /proc/mounts."""

    def __init__(self) -> None:
        self.mounted: set[str] = set()
        self.commands: list[list[str]] = []

    def run(self, command: list[str]) -> None:
        self.commands.append(list(command))
        if command[0] == "mount":
            self.mounted.add(command[-1])
        elif command[0] == "umount":
            self.mounted.discard(command[-1])

    def is_mounted(self, path: Path, fs_type: str | None = None) -> bool:
        del fs_type
        return str(path) in self.mounted

    @staticmethod
    def rmdir(path: Path) -> None:
        """configfs semantics on a plain tree: attribute files vanish with their node."""
        if path.is_dir():
            for child in path.iterdir():
                if child.is_file() and not child.is_symlink():
                    child.unlink()
        path.rmdir()


@pytest.fixture
def system(monkeypatch: pytest.MonkeyPatch) -> FakeSystem:
    fake = FakeSystem()
    monkeypatch.setattr(gadget_module, "_run", fake.run)
    monkeypatch.setattr(gadget_module, "_is_mounted", fake.is_mounted)
    monkeypatch.setattr(gadget_module, "_rmdir", fake.rmdir)
    monkeypatch.setattr(gadget_module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(ConfigFSGadget, "ensure_configfs", lambda self: None)
    monkeypatch.setattr(ConfigFSGadget, "available_udcs", lambda self: ["fake.udc"])
    return fake


@pytest.fixture
def configfs(tmp_path: Path) -> Path:
    root = tmp_path / "usb_gadget"
    root.mkdir()
    return root


def make_gadget(configfs: Path, tmp_path: Path, name: str = "hccast") -> ConfigFSGadget:
    return ConfigFSGadget(
        name=name,
        function_name=name,
        mountpoint=tmp_path / f"ffs-{name}",
        configfs_root=configfs,
    )


def start(gadget: ConfigFSGadget) -> None:
    gadget.create(
        vendor_id=0x18D1,
        product_id=0x2D00,
        manufacturer="Android",
        product="Android Accessory",
        serial="TEST-001",
    )
    gadget.mount_functionfs()
    gadget.link_function()
    gadget.bind()


def snapshot(gadget: ConfigFSGadget, system: FakeSystem) -> dict[str, object]:
    return {
        "root": gadget.root.is_dir(),
        "function": gadget.function.is_dir(),
        "link": gadget.link.is_symlink(),
        "udc": (gadget.root / "UDC").read_text() if (gadget.root / "UDC").exists() else None,
        "mounted": str(gadget.mountpoint) in system.mounted,
        "mountpoint": gadget.mountpoint.is_dir(),
    }


def test_first_start_owns_every_resource_it_creates(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    gadget = make_gadget(configfs, tmp_path)

    start(gadget)

    assert gadget.owned_resources == {
        "root": True,
        "function": True,
        "mountpoint": True,
        "functionfs_mount": True,
        "link": True,
        "udc_binding": True,
    }
    assert snapshot(gadget, system) == {
        "root": True,
        "function": True,
        "link": True,
        "udc": "fake.udc",
        "mounted": True,
        "mountpoint": True,
    }


def test_rejected_second_start_leaves_the_original_gadget_untouched(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    first = make_gadget(configfs, tmp_path)
    start(first)
    before = snapshot(first, system)
    commands_before = list(system.commands)

    second = make_gadget(configfs, tmp_path)
    with pytest.raises(GadgetError, match="already exists"):
        second.create(
            vendor_id=0x18D1,
            product_id=0x2D00,
            manufacturer="Android",
            product="Android Accessory",
            serial="TEST-002",
        )
    assert not any(second.owned_resources.values())

    # The CLI's finally block always calls cleanup; it must be a no-op here.
    second.cleanup()

    assert snapshot(first, system) == before
    assert system.commands == commands_before
    assert first.owned_resources["udc_binding"] is True

    first.cleanup()
    assert snapshot(first, system) == {
        "root": False,
        "function": False,
        "link": False,
        "udc": None,
        "mounted": False,
        "mountpoint": False,
    }
    assert not any(first.owned_resources.values())


def test_cli_second_start_is_rejected_without_disconnecting_the_first_session(
    system: FakeSystem, configfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = make_gadget(configfs, tmp_path)
    start(first)
    before = snapshot(first, system)

    class TestGadget(ConfigFSGadget):
        def __init__(self, **kwargs: object) -> None:
            kwargs["configfs_root"] = configfs
            super().__init__(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "ConfigFSGadget", TestGadget)
    args = argparse.Namespace(
        gadget_name="hccast",
        function_name="hccast",
        mountpoint=str(tmp_path / "ffs-hccast"),
        udc=None,
        aoa_mode="direct",
        serial="TEST-002",
        aoa_timeout=1.0,
        enable_timeout=1.0,
        reenumeration_delay=0.0,
        handshake_timeout=1.0,
        send_settings=False,
    )

    with pytest.raises(GadgetError, match="already exists"):
        cli._run_gadget(args, stream=False)

    assert snapshot(first, system) == before
    assert not any(command[0] == "umount" for command in system.commands)


def test_partially_created_gadget_removes_only_what_it_made(
    system: FakeSystem, configfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gadget = make_gadget(configfs, tmp_path)
    real_write = gadget_module._write

    def failing_write(path: Path, value: str) -> None:
        if path.name == "bcdDevice":
            raise GadgetError("simulated configfs write failure")
        real_write(path, value)

    monkeypatch.setattr(gadget_module, "_write", failing_write)
    with pytest.raises(GadgetError, match="simulated"):
        start(gadget)
    assert gadget.owned_resources["root"] is True
    assert gadget.owned_resources["function"] is False

    gadget.cleanup()

    assert not gadget.root.exists()
    assert not any(gadget.owned_resources.values())
    assert system.commands == []


def test_preexisting_mount_and_link_are_adopted_but_never_released(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    stale = make_gadget(configfs, tmp_path)
    start(stale)
    stale_state = snapshot(stale, system)
    # Simulate a later process that reuses the existing mount and link but
    # creates nothing itself.
    adopter = make_gadget(configfs, tmp_path)
    adopter.mount_functionfs()
    adopter.link_function()

    assert adopter.owned_resources["functionfs_mount"] is False
    assert adopter.owned_resources["link"] is False
    adopter.cleanup()
    assert snapshot(stale, system) == stale_state


def test_force_cleanup_is_the_explicit_stale_state_removal(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    stale = make_gadget(configfs, tmp_path)
    start(stale)
    assert snapshot(stale, system)["udc"] == "fake.udc"

    ConfigFSGadget.force_cleanup(
        name="hccast",
        function_name="hccast",
        mountpoint=tmp_path / "ffs-hccast",
        configfs_root=configfs,
    )

    assert snapshot(stale, system) == {
        "root": False,
        "function": False,
        "link": False,
        "udc": None,
        "mounted": False,
        "mountpoint": False,
    }
    assert ["umount", str(tmp_path / "ffs-hccast")] in system.commands


# --- Observable cleanup ---------------------------------------------------------


def test_cleanup_report_lists_actions_and_verified_postconditions(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    gadget = make_gadget(configfs, tmp_path)
    start(gadget)

    report = gadget.cleanup()

    assert report.success is True
    assert report.attempted_actions == (
        "udc-unbind",
        "link-remove",
        "functionfs-unmount",
        "mountpoint-remove",
        "function-remove",
        "configfs-remove",
    )
    assert report.errors == ()
    assert report.verified_postconditions == (
        "udc-released",
        "functionfs-unmounted",
        "mountpoint-removed",
        "function-removed",
        "gadget-removed",
    )
    assert report.failed_postconditions == ()
    assert report.to_mapping()["success"] is True


def test_cleanup_without_owned_resources_reports_nothing_to_verify(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    report = make_gadget(configfs, tmp_path).cleanup()

    assert report.success is True
    assert report.attempted_actions == ()
    assert report.verified_postconditions == ()


def test_failed_unmount_is_reported_not_suppressed(
    system: FakeSystem, configfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gadget = make_gadget(configfs, tmp_path)
    start(gadget)

    def refuse_umount(command: list[str]) -> None:
        if command[0] == "umount":
            raise GadgetError("umount: target is busy")
        system.run(command)

    monkeypatch.setattr(gadget_module, "_run", refuse_umount)

    report = gadget.cleanup()

    assert report.success is False
    assert [(e.action, e.message) for e in report.errors] == [
        ("functionfs-unmount", "umount: target is busy")
    ]
    assert "functionfs-unmounted" in report.failed_postconditions
    assert "mountpoint-removed" in report.failed_postconditions
    assert "udc-released" in report.verified_postconditions
    assert gadget.owned_resources["functionfs_mount"] is True  # still ours to release


def test_lingering_gadget_root_fails_the_gadget_removed_postcondition(
    system: FakeSystem, configfs: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gadget = make_gadget(configfs, tmp_path)
    start(gadget)

    def refuse_root(path: Path) -> None:
        if path == gadget.root:
            raise OSError(16, "Device or resource busy")
        system.rmdir(path)

    monkeypatch.setattr(gadget_module, "_rmdir", refuse_root)

    report = gadget.cleanup()

    assert report.success is False
    assert [e.action for e in report.errors] == ["configfs-remove"]
    assert report.failed_postconditions == ("gadget-removed",)
    assert gadget.root.is_dir()


def test_stale_state_removal_verifies_owner_mount_and_gadget_postconditions(
    system: FakeSystem, configfs: Path, tmp_path: Path
) -> None:
    stale = make_gadget(configfs, tmp_path)
    start(stale)

    report = ConfigFSGadget.force_cleanup(
        name="hccast", function_name="hccast", mountpoint=tmp_path / "ffs-hccast", configfs_root=configfs
    )

    assert report.success is True
    assert report.verified_postconditions == (
        "udc-released",
        "functionfs-unmounted",
        "mountpoint-removed",
        "function-removed",
        "gadget-removed",
    )
    again = ConfigFSGadget.force_cleanup(
        name="hccast", function_name="hccast", mountpoint=tmp_path / "ffs-hccast", configfs_root=configfs
    )
    assert again.success is True
    assert again.attempted_actions == ("link-remove",)


def _patch_cli_gadget(monkeypatch: pytest.MonkeyPatch, configfs: Path) -> None:
    class TestGadget(ConfigFSGadget):
        def __init__(self, **kwargs: object) -> None:
            kwargs["configfs_root"] = configfs
            super().__init__(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "ConfigFSGadget", TestGadget)


def test_gadget_stop_exits_nonzero_with_a_structured_report_when_state_remains(
    system: FakeSystem,
    configfs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    stale = make_gadget(configfs, tmp_path)
    start(stale)
    _patch_cli_gadget(monkeypatch, configfs)

    def refuse_umount(command: list[str]) -> None:
        if command[0] == "umount":
            raise GadgetError("umount: target is busy")
        system.run(command)

    monkeypatch.setattr(gadget_module, "_run", refuse_umount)
    arguments = ["gadget-stop", "--mountpoint", str(tmp_path / "ffs-hccast")]

    assert cli.main(arguments) == cli.EXIT_CLEANUP_INCOMPLETE
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "gadget-stop cleanup incomplete" in captured.err
    report = json.loads(captured.err.split("\n", 1)[1])
    assert report["operation"] == "gadget-stop"
    assert report["cleanup"]["success"] is False
    assert report["cleanup"]["errors"] == [
        {"action": "functionfs-unmount", "message": "umount: target is busy"}
    ]
    assert "functionfs-unmounted" in report["cleanup"]["failed_postconditions"]

    monkeypatch.setattr(gadget_module, "_run", system.run)
    assert cli.main(arguments) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["cleanup"]["success"] is True
    assert report["cleanup"]["failed_postconditions"] == []


@dataclass
class _Info:
    product: str = "HCT-AT01"
    version: str = "2505161526"


def _fake_session_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeControl:
        def __init__(self, mountpoint: object, *, receive_all_control: bool) -> None:
            del mountpoint, receive_all_control

        def open(self) -> None:
            pass

        def wait_for_enable(self, *, timeout_s: float) -> None:
            del timeout_s

        def close(self) -> None:
            pass

    class FakeTransport:
        def __init__(self, mountpoint: object) -> None:
            del mountpoint

        def close(self) -> None:
            pass

    class FakeSession:
        def __init__(self, transport: object) -> None:
            del transport

        def handshake(self, *, timeout_s: float) -> _Info:
            del timeout_s
            return _Info()

        def close(self) -> None:
            pass

    monkeypatch.setattr(cli, "FunctionFSControl", FakeControl)
    monkeypatch.setattr(cli, "FunctionFSTransport", FakeTransport)
    monkeypatch.setattr(cli, "HCCASTSession", FakeSession)


def _gadget_args(tmp_path: Path) -> list[str]:
    return [
        "gadget-handshake",
        "--aoa-mode",
        "direct",
        "--mountpoint",
        str(tmp_path / "ffs-hccast"),
        "--serial",
        "TEST-003",
    ]


def test_completed_gadget_session_exits_nonzero_when_cleanup_is_incomplete(
    system: FakeSystem,
    configfs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_cli_gadget(monkeypatch, configfs)
    _fake_session_stack(monkeypatch)

    assert cli.main(_gadget_args(tmp_path)) == 0
    assert "HCCAST handshake complete" in capsys.readouterr().out

    def refuse_umount(command: list[str]) -> None:
        if command[0] == "umount":
            raise GadgetError("umount: target is busy")
        system.run(command)

    monkeypatch.setattr(gadget_module, "_run", refuse_umount)

    assert cli.main(_gadget_args(tmp_path)) == cli.EXIT_CLEANUP_INCOMPLETE
    captured = capsys.readouterr()
    assert "HCCAST handshake complete" in captured.out
    assert "gadget-handshake cleanup incomplete" in captured.err
    report = json.loads(captured.err.split("\n", 1)[1])
    assert report["cleanup"]["errors"][0]["action"] == "functionfs-unmount"
    assert "functionfs-unmounted" in report["cleanup"]["failed_postconditions"]


def test_failed_gadget_session_still_prints_its_cleanup_report(
    system: FakeSystem,
    configfs: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_cli_gadget(monkeypatch, configfs)
    _fake_session_stack(monkeypatch)

    def failing_wait(self: object, *, timeout_s: float) -> None:
        del self, timeout_s
        raise TimeoutError("screen never enabled the function")

    monkeypatch.setattr(cli.FunctionFSControl, "wait_for_enable", failing_wait)

    def refuse_umount(command: list[str]) -> None:
        if command[0] == "umount":
            raise GadgetError("umount: target is busy")
        system.run(command)

    monkeypatch.setattr(gadget_module, "_run", refuse_umount)

    assert cli.main(_gadget_args(tmp_path)) == 1
    err = capsys.readouterr().err
    assert "screen never enabled the function" in err
    assert "gadget-handshake cleanup incomplete" in err
    assert '"functionfs-unmount"' in err
