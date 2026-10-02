"""Inspectable synthetic package payloads; no install or native worker execution."""

import hashlib
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools.build_package import PackageError, VERSION, build_package
from packaging_tools.source_bundle import SOURCE_FILES, build_bundle

APP = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def elf():
    # Structurally valid synthetic ELF container, never a runnable worker claim.
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header = struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0x1000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 120, 0x1000, 0x1000, 4, 4, 4)
    return ident + header + segment + b"\0" * 4


@pytest.fixture
def inputs(tmp_path):
    project = tmp_path / "project"
    for relative in SOURCE_FILES:
        path = project / "apps/panelbridge" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((APP / relative).read_bytes())
    source = build_bundle(project, "source").path
    worker = project / "worker"
    worker.write_bytes(elf())
    return project, source, worker


def assemble(inputs, output="artifacts", **options):
    project, source, worker = inputs
    return build_package(project, output, source_bundle=source, worker=worker,
                         build_deb=options.pop("build_deb", False), **options)


def test_payload_has_fixed_installed_paths_modes_bytes_and_matching_source(inputs):
    project, source, worker = inputs
    result = assemble(inputs)
    root = result.payload_dir
    expected = {
        "usr/bin/panelbridge": ("packaging/panelbridge", 0o755),
        "usr/lib/panelbridge/session-launch": ("packaging/session-launch", 0o755),
        "usr/lib/panelbridge/helper-launch.py": ("packaging/helper-launch.py", 0o644),
        "usr/lib/panelbridge/panelbridge/controller.py": ("panelbridge/controller.py", 0o644),
        "usr/lib/panelbridge/helper/network.py": ("helper/network.py", 0o644),
        "usr/lib/panelbridge/packaging_tools/user_setup.py": ("packaging_tools/user_setup.py", 0o644),
        "usr/lib/panelbridge/packaging_tools/user_maintenance.py": ("packaging_tools/user_maintenance.py", 0o644),
        "usr/lib/panelbridge/maintain-user-launch.py": ("packaging/maintain-user-launch.py", 0o644),
        "usr/lib/panelbridge/maintain-launch.py": ("packaging/maintain-launch.py", 0o644),
        "usr/lib/panelbridge/packaging_tools/maintenance.py": ("packaging_tools/maintenance.py", 0o644),
        "usr/lib/panelbridge/packaging_tools/apt_plan.py": ("packaging_tools/apt_plan.py", 0o644),
        "usr/lib/systemd/system/panelbridge-helper.service": ("packaging/panelbridge-helper.service", 0o644),
        "usr/lib/systemd/system/panelbridge-rescue.service": ("packaging/panelbridge-rescue.service", 0o644),
        "usr/share/dbus-1/system.d/org.panelbridge.Helper1.conf": ("packaging/org.panelbridge.Helper1.conf", 0o644),
        "usr/share/applications/org.panelbridge.PanelBridge.desktop": ("packaging/org.panelbridge.PanelBridge.desktop", 0o644),
        "usr/share/panelbridge/LICENSE": ("LICENSE", 0o644),
        "usr/share/panelbridge/THIRD_PARTY_NOTICES.md": ("THIRD_PARTY_NOTICES.md", 0o644),
        "usr/share/icons/hicolor/scalable/apps/org.panelbridge.PanelBridge.svg": (
            "assets/icons/hicolor/scalable/apps/org.panelbridge.PanelBridge.svg", 0o644),
    }
    for installed, (original, mode) in expected.items():
        path = root / installed
        assert path.read_bytes() == (project / "apps/panelbridge" / original).read_bytes()
        assert path.stat().st_mode & 0o777 == mode
    assert (root / "usr/lib/panelbridge/bin/panelbridge-wfd-worker").read_bytes() == worker.read_bytes()
    assert (root / "usr/lib/panelbridge/bin/panelbridge-wfd-worker").stat().st_mode & 0o777 == 0o755
    assert (root / "usr/share/panelbridge/panelbridge-app-source.tar.gz").read_bytes() == source.read_bytes()
    assert not (root / "etc").exists()
    assert not (root / "var").exists()
    assert {path.name for path in (root / "DEBIAN").iterdir()} == {"control", "md5sums", "postinst", "prerm", "postrm"}
    for name in ("postinst", "prerm", "postrm"):
        hook = root / "DEBIAN" / name
        assert hook.read_bytes() == (APP / "packaging/debian" / name).read_bytes()
        assert hook.stat().st_mode & 0o777 == 0o755
        assert subprocess.run(["/bin/sh", "-n", str(hook)], capture_output=True).returncode == 0
    installed = {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*")
                 if p.is_file() and "DEBIAN" not in p.relative_to(root).parts}
    checksums = (root / "DEBIAN/md5sums").read_text().splitlines()
    assert checksums == [f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {name}"
                         for name, data in sorted(installed.items())]
    assert f"Installed-Size: {(sum(map(len, installed.values())) + 1023) // 1024}\n" in (root / "DEBIAN/control").read_text()
    assert not any(path.is_symlink() for path in root.rglob("*"))
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt["source_snapshot_matches"] is True
    assert receipt["worker_source_match_verified"] is False
    assert receipt["dependency_proof_status"] == "incomplete"
    assert receipt["ready_for_install"] is False
    assert receipt["release_ready"] is False
    assert receipt["source_sha256"] == digest(source.read_bytes())
    assert receipt["worker_sha256"] == digest(worker.read_bytes())
    actual = {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}
    assert actual == {item["path"] for item in receipt["payload_files"]}
    for item in receipt["payload_files"]:
        path = root / item["path"]
        assert digest(path.read_bytes()) == item["sha256"]
        assert path.stat().st_mode & 0o777 == item["mode"]
        assert int(path.stat().st_mtime) == 0


def test_runtime_dependencies_include_nonlinked_tools_plugins_and_pycairo(inputs):
    result = assemble(inputs)
    control = (result.payload_dir / "DEBIAN/control").read_text()
    assert f"Package: panelbridge\nVersion: {VERSION}\nArchitecture: arm64\n" in control
    depends = next(line for line in control.splitlines() if line.startswith("Depends: "))
    names = {item.strip().split()[0] for item in depends[9:].split(",")}
    assert {"python3", "python3-gi", "python3-cairo", "gir1.2-gtk-4.0", "libgtk-4-1",
            "wf-recorder", "wlr-randr", "kanshi", "network-manager", "raspi-utils",
            "gstreamer1.0-plugins-base", "gstreamer1.0-plugins-good", "gstreamer1.0-plugins-bad",
            "gstreamer1.0-plugins-ugly", "gstreamer1.0-libav", "libgstreamer1.0-0",
            "libgstrtspserver-1.0-0", "iw", "iproute2", "dbus", "systemd", "labwc"} <= names
    assert not {"gcc", "meson", "ninja-build", "python3-pip"} & names


def test_payload_and_receipt_are_reproducible_across_output_and_umask(inputs):
    first = assemble(inputs, "one")
    old = os.umask(0o077)
    try:
        second = assemble(inputs, "two")
    finally:
        os.umask(old)
    assert first.receipt_path.read_bytes() == second.receipt_path.read_bytes()
    for path in first.payload_dir.rglob("*"):
        twin = second.payload_dir / path.relative_to(first.payload_dir)
        assert path.stat().st_mode & 0o777 == twin.stat().st_mode & 0o777
        if path.is_file():
            assert path.read_bytes() == twin.read_bytes()


@pytest.mark.parametrize("offset,value", [(0, 0), (4, 1), (5, 2), (6, 0), (18, 62), (54, 0)])
def test_wrong_or_malformed_elf_is_rejected_before_output(inputs, offset, value):
    data = bytearray(inputs[2].read_bytes())
    data[offset] = value
    inputs[2].write_bytes(data)
    with pytest.raises(PackageError, match="ELF|AArch64"):
        assemble(inputs)
    assert not (inputs[0] / "artifacts").exists()


def test_stale_current_source_or_tampered_bundle_is_not_matching_source(inputs):
    source_file = inputs[0] / "apps/panelbridge/panelbridge/models.py"
    before = source_file.read_bytes()
    source_file.write_bytes(before + b"\n# Changed after source freeze\n")
    with pytest.raises(PackageError, match="source|snapshot|manifest"):
        assemble(inputs)
    source_file.write_bytes(before)
    inputs[1].write_bytes(inputs[1].read_bytes() + b"extra")
    with pytest.raises(PackageError, match="source|snapshot|manifest"):
        assemble(inputs)


@pytest.mark.parametrize("target", ["worker", "source", "app"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_unsafe_inputs_fail_before_output(inputs, tmp_path, target, kind):
    path = {"worker": inputs[2], "source": inputs[1],
            "app": inputs[0] / "apps/panelbridge/panelbridge/models.py"}[target]
    saved = tmp_path / "saved"
    saved.write_bytes(path.read_bytes())
    path.unlink()
    if kind == "symlink":
        path.symlink_to(saved)
    elif kind == "hardlink":
        os.link(saved, path)
    else:
        os.mkfifo(path)
    with pytest.raises(PackageError):
        assemble(inputs)
    assert not (inputs[0] / "artifacts").exists()


def test_worker_embedded_private_build_path_is_refused_without_echoing_it(inputs):
    private = ("/" + "home" + "/" + "private-builder" + "/project").encode()
    inputs[2].write_bytes(elf() + private)
    with pytest.raises(PackageError) as raised:
        assemble(inputs)
    assert private.decode() not in str(raised.value)
    assert not (inputs[0] / "artifacts").exists()


def test_output_cannot_escape_follow_aliases_or_overwrite(inputs, tmp_path):
    first = assemble(inputs)
    before = first.receipt_path.read_bytes()
    with pytest.raises(PackageError, match="exist"):
        assemble(inputs)
    assert first.receipt_path.read_bytes() == before
    outside = tmp_path / "outside"
    outside.mkdir()
    (inputs[0] / "alias").symlink_to(outside, target_is_directory=True)
    for output in (outside, "../outside", "alias"):
        with pytest.raises(PackageError):
            assemble(inputs, output)
    assert list(outside.iterdir()) == []


def test_missing_dpkg_returns_payload_without_claiming_a_deb(inputs, monkeypatch):
    monkeypatch.setattr("packaging_tools.build_package.shutil.which", lambda name: None)
    result = assemble(inputs, build_deb=True)
    assert result.deb_path is None
    assert result.deb_status == "dpkg_deb_unavailable"
    assert result.payload_dir.is_dir()
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt["deb_sha256"] is None


@pytest.mark.parametrize("failure", ["timeout", "nonzero", "no_output"])
def test_dpkg_failure_keeps_payload_and_does_not_claim_success(inputs, monkeypatch, failure):
    monkeypatch.setattr("packaging_tools.build_package.shutil.which", lambda name: "/usr/bin/dpkg-deb")

    def run(args, **kwargs):
        assert args[1:3] == ["--root-owner-group", "--build"]
        assert kwargs["timeout"] <= 120
        assert kwargs.get("shell", False) is False
        assert kwargs["env"]["SOURCE_DATE_EPOCH"] == "0"
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        return subprocess.CompletedProcess(args, 2 if failure == "nonzero" else 0, b"", b"")

    monkeypatch.setattr("packaging_tools.build_package.subprocess.run", run)
    result = assemble(inputs, build_deb=True)
    assert result.deb_path is None
    assert result.deb_status == "dpkg_deb_failed"
    assert result.payload_dir.is_dir()
    assert json.loads(result.receipt_path.read_bytes())["ready_for_install"] is False


def test_shlibdeps_proof_is_bound_to_exact_worker_and_merged(inputs):
    proof = inputs[0] / "shlibdeps.json"
    value = {"schema_version": 1, "tool": "dpkg-shlibdeps", "architecture": "arm64", "exit_code": 0,
             "worker_sha256": digest(inputs[2].read_bytes()), "depends": "libc6 (>= 2.38), libnm0 (>= 1.52)"}
    proof.write_text(json.dumps(value))
    result = assemble(inputs, shlibdeps=proof)
    receipt = json.loads(result.receipt_path.read_bytes())
    assert receipt["dependency_proof_status"] == "native_shlibdeps_supplied"
    assert "libc6 (>= 2.38)" in (result.payload_dir / "DEBIAN/control").read_text()
    assert receipt["worker_source_match_verified"] is False
    value["worker_sha256"] = "0" * 64
    proof.write_text(json.dumps(value))
    with pytest.raises(PackageError, match="shlibdeps|worker"):
        assemble(inputs, "wrong-proof", shlibdeps=proof)


def test_real_or_effective_root_is_rejected(inputs, monkeypatch):
    monkeypatch.setattr("packaging_tools.build_package.os.geteuid", lambda: 0)
    with pytest.raises(PackageError, match="root|unprivileged"):
        assemble(inputs)


def deb_from_stage(root, *, damaged=None):
    """Test-only ar/tar serialization, independent of the assembler's writer."""
    members = {"debian-binary": b"2.0\n"}
    for name, control in (("control.tar.gz", True), ("data.tar.gz", False)):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                if damaged == "wrong_archive":
                    if control:
                        continue
                elif (path.relative_to(root).parts[0] == "DEBIAN") != control:
                    continue
                relative = path.relative_to(root / "DEBIAN" if control else root).as_posix()
                data = path.read_bytes()
                member = tarfile.TarInfo("./" + relative)
                member.size = len(data)
                member.mode = path.stat().st_mode & 0o777
                if relative == "usr/bin/panelbridge":
                    if damaged == "bytes":
                        data = b"!" + data[1:]
                    if damaged == "owner":
                        member.uid = 1000
                    if damaged == "mode":
                        member.mode = 0o777
                archive.addfile(member, io.BytesIO(data))
            if damaged == "extra_directory" and not control:
                extra = tarfile.TarInfo("./etc")
                extra.type = tarfile.DIRTYPE
                extra.mode = 0o755
                archive.addfile(extra)
        members[name] = output.getvalue()
    result = b"!<arch>\n"
    for name, data in members.items():
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        result += header + data + (b"\n" if len(data) % 2 else b"")
    return result


@pytest.mark.parametrize("damage", [None, "bytes", "owner", "mode", "extra_directory", "wrong_archive"])
def test_simulated_dpkg_output_is_inspected_against_actual_staged_payload(inputs, monkeypatch, damage):
    monkeypatch.setattr("packaging_tools.build_package.shutil.which", lambda name: "/usr/bin/dpkg-deb")

    def run(args, **kwargs):
        assert kwargs["timeout"] == 120
        Path(args[-1]).write_bytes(deb_from_stage(Path(args[-2]), damaged=damage))
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr("packaging_tools.build_package.subprocess.run", run)
    result = assemble(inputs, build_deb=True)
    receipt = json.loads(result.receipt_path.read_bytes())
    if damage:
        assert result.deb_status == "dpkg_deb_failed"
        assert result.deb_path is None
        assert receipt["deb_sha256"] is None
    else:
        assert result.deb_status == "built"
        assert receipt["deb_sha256"] == digest(result.deb_path.read_bytes())
    assert receipt["ready_for_install"] is (result.deb_status == "built")


def test_group_writable_output_ancestor_is_rejected(inputs):
    output = inputs[0] / "shared"
    output.mkdir(mode=0o770)
    output.chmod(0o770)
    with pytest.raises(PackageError, match="writable|directory"):
        assemble(inputs, "shared/new")
    assert not (output / "new").exists()


def test_actual_dpkg_archive_when_tool_is_available(inputs):
    import shutil
    executable = shutil.which("dpkg-deb")
    if not executable:
        pytest.skip("dpkg-deb is not installed; payload checks run on this host")
    result = assemble(inputs, build_deb=True)
    assert result.deb_status == "built"
    inspected = subprocess.run([executable, "--contents", str(result.deb_path)],
                               check=True, capture_output=True, text=True, timeout=30)
    assert "root/root" in inspected.stdout
    assert "usr/lib/panelbridge/bin/panelbridge-wfd-worker" in inspected.stdout
