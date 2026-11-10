"""Synthetic release sets with a structural ELF; real dpkg-deb when present, never sudo."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools import build_package as package
from packaging_tools import release
from packaging_tools import source_bundle as source
from packaging_tools.release import MANIFEST_NAME, ReleaseError, build_release, inspect_release, inspect_worker

APP = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def elf(tail=b""):
    # Structurally valid synthetic AArch64 container, never a runnable worker claim.
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header = struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0x1000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 120, 0x1000, 0x1000, 4, 4, 4)
    return ident + header + segment + b"\0" * 4 + tail


def synthetic_deb(payload):
    members = {"debian-binary": b"2.0\n"}
    for section in ("control", "data"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as output:
            for name, (data, mode) in sorted(payload.items()):
                control = name.startswith("DEBIAN/")
                if control != (section == "control"):
                    continue
                info = tarfile.TarInfo(name.removeprefix("DEBIAN/") if control else name)
                info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, 0, 0, 0
                output.addfile(info, io.BytesIO(data))
        members[section + ".tar.gz"] = stream.getvalue()
    result = b"!<arch>\n"
    for name, data in members.items():
        result += f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        result += data + (b"\n" if len(data) % 2 else b"")
    return result


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    for relative in source.SOURCE_FILES:
        target = root / "apps/panelbridge" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((APP / relative).read_bytes())
    return root


@pytest.fixture
def worker(project):
    path = project / "worker"
    path.write_bytes(elf())
    return path


@pytest.fixture
def fake_dpkg(monkeypatch):
    def build(build_dir, payload_dir, payload, enabled):
        assert enabled
        path = build_dir / f"{package.NAME}.deb"
        path.write_bytes(synthetic_deb(payload))
        return path, "built", digest(path.read_bytes())

    monkeypatch.setattr(package, "_build_deb", build)


def read_manifest(result):
    manifest = json.loads(result.manifest_path.read_bytes())
    assert digest(result.manifest_path.read_bytes()) == result.manifest_sha256
    return manifest


def test_full_release_set_is_cross_checked_and_never_release_ready(project, worker, fake_dpkg):
    result = build_release(project, "release", worker=worker)
    manifest = read_manifest(result)

    assert result.stage == manifest["stage"] == "installer"
    assert result.release_ready is False
    assert manifest["version"] == package.VERSION
    assert manifest["claims"] == {
        "release_ready": False,
        "release_gate": "experimental_first_install",
        "physical_acceptance": "not_established_by_this_build; see docs/PI500.md",
        "software_verification": "artifact_cross_check",
        "legacy_wheel_delivers_panelbridge": False,
        "complete_binary_dependency_source": False,
    }
    artifacts = manifest["artifacts"]
    assert set(artifacts) == {"source_bundle", "package", "receipt", "installer"}
    for entry in artifacts.values():
        assert entry["sha256"] == digest((project / entry["path"]).read_bytes())
    assert artifacts["package"]["worker_sha256"] == digest(worker.read_bytes())
    assert artifacts["package"]["dependency_proof_status"] == "incomplete"
    assert artifacts["source_bundle"]["upstream"]["sha256"] == source.UPSTREAM_SHA256
    assert artifacts["source_bundle"]["file_count"] == len(source.SOURCE_FILES) + 2
    assert artifacts["source_bundle"]["notices"] == {
        "LICENSE": digest((APP / "LICENSE").read_bytes()),
        "THIRD_PARTY_NOTICES.md": digest((APP / "THIRD_PARTY_NOTICES.md").read_bytes()),
    }
    assert (project / artifacts["installer"]["path"]).name == f"panelbridge_{package.VERSION}_arm64_experimental.run"
    assert str(project).encode() not in (project / artifacts["installer"]["path"]).read_bytes()
    assert (result.output_dir / MANIFEST_NAME).stat().st_mode & 0o777 == 0o600


def test_package_carries_matching_sender_source_and_notices(project, worker, fake_dpkg):
    result = build_release(project, "release", worker=worker)
    artifacts = read_manifest(result)["artifacts"]
    deb = (project / artifacts["package"]["path"]).read_bytes()
    bundle = (project / artifacts["source_bundle"]["path"]).read_bytes()

    members = release.installer._deb_files(deb)
    assert members[release.SOURCE_PATH][0] == bundle
    assert members[release.WORKER_PATH][0] == worker.read_bytes()
    for installed, original in release.NOTICE_PATHS.items():
        assert members[installed][0] == (APP / original).read_bytes()
    with tarfile.open(fileobj=io.BytesIO(bundle), mode="r:gz") as archive:
        names = {member.name.removeprefix(source.PREFIX) for member in archive.getmembers()}
    assert release.PATCHED_SENDER_SOURCES <= names
    embedded = release._installer_payload((project / artifacts["installer"]["path"]).read_bytes())
    assert embedded == {
        "package.deb": deb,
        "source.tar.gz": bundle,
        "package-receipt.json": (project / artifacts["receipt"]["path"]).read_bytes(),
    }


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="dpkg-deb is not installed")
def test_real_dpkg_deb_release_set_when_the_tool_is_available(project, worker):
    result = build_release(project, "release", worker=worker)
    manifest = read_manifest(result)

    assert manifest["stage"] == "installer"
    deb = (project / manifest["artifacts"]["package"]["path"]).read_bytes()
    assert deb.startswith(b"!<arch>\n")
    assert release.installer._deb_files(deb)[release.SOURCE_PATH][0] == (
        project / manifest["artifacts"]["source_bundle"]["path"]
    ).read_bytes()


def test_source_only_release_builds_and_inspects_the_bundle(project):
    result = build_release(project, "release")
    manifest = read_manifest(result)

    assert result.stage == manifest["stage"] == "source_bundle"
    assert set(manifest["artifacts"]) == {"source_bundle"}
    assert manifest["claims"]["software_verification"] == "source_bundle_only"
    assert manifest["claims"]["release_ready"] is False
    bundle = manifest["artifacts"]["source_bundle"]
    assert bundle["sha256"] == digest((project / bundle["path"]).read_bytes())
    assert bundle["upstream"]["commit"] == source.UPSTREAM_COMMIT
    assert not (result.output_dir / "package").exists()
    assert not (result.output_dir / "installer").exists()


@pytest.mark.parametrize("tail, reason", [
    (b"/" + b"home" + b"/" + b"private-person" + b"/panelbridge", "rejected"),
    (b"/srv/checkout/vendor/panelbridge-wfd-graceful-build/build/src", "development build paths"),
    (b"/tmp/build-20261105/src", "development build paths"),
    (b".debug_info", "debug records"),
    (b".debug_str", "debug records"),
])
def test_development_or_unstripped_worker_is_refused_without_a_manifest(project, fake_dpkg, tail, reason):
    worker = project / "worker"
    worker.write_bytes(elf(tail))

    with pytest.raises(ReleaseError, match=reason) as raised:
        build_release(project, "release", worker=worker)

    assert b"private-person" not in str(raised.value).encode()
    assert not (project / "release").exists()  # refused before any artifact is written


def test_worker_refusal_is_repeated_on_the_packaged_copy(project, worker, fake_dpkg):
    result = build_release(project, "release", worker=worker)
    artifacts = read_manifest(result)["artifacts"]
    deb = (project / artifacts["package"]["path"]).read_bytes()
    members = release.installer._deb_files(deb)
    unstripped = elf(b".debug_info")
    members[release.WORKER_PATH] = (unstripped, 0o755)
    tampered = synthetic_deb(members)
    receipt = json.loads((project / artifacts["receipt"]["path"]).read_bytes())
    receipt.update(deb_sha256=digest(tampered), worker_sha256=digest(unstripped))

    with pytest.raises(ReleaseError, match="debug records"):
        inspect_release(
            source_bytes=(project / artifacts["source_bundle"]["path"]).read_bytes(),
            deb_bytes=tampered,
            receipt_bytes=source._json(receipt),
            installer_bytes=(project / artifacts["installer"]["path"]).read_bytes(),
        )


def test_inspect_worker_accepts_a_clean_synthetic_elf_and_rejects_non_elf():
    assert inspect_worker(elf()) == digest(elf())
    with pytest.raises(ReleaseError, match="rejected"):
        inspect_worker(b"#!/bin/sh\n")


def test_tampered_artifacts_fail_cross_check(project, worker, fake_dpkg):
    result = build_release(project, "release", worker=worker)
    artifacts = read_manifest(result)["artifacts"]
    original = {
        "source_bytes": (project / artifacts["source_bundle"]["path"]).read_bytes(),
        "deb_bytes": (project / artifacts["package"]["path"]).read_bytes(),
        "receipt_bytes": (project / artifacts["receipt"]["path"]).read_bytes(),
        "installer_bytes": (project / artifacts["installer"]["path"]).read_bytes(),
    }
    assert inspect_release(**original)["installer"]["sha256"] == artifacts["installer"]["sha256"]

    with pytest.raises(ReleaseError, match="receipt"):
        inspect_release(**{**original, "deb_bytes": original["deb_bytes"] + b"\n"})
    receipt = json.loads(original["receipt_bytes"])
    receipt["release_ready"] = True
    with pytest.raises(ReleaseError, match="receipt"):
        inspect_release(**{**original, "receipt_bytes": source._json(receipt)})
    with pytest.raises(ReleaseError, match="Installer"):
        inspect_release(**{**original, "installer_bytes": original["installer_bytes"].replace(
            b'PAYLOAD_B64 = "', b'PAYLOAD_B64 = "AAAA', 1)})
    other = build_release(project, "other", worker=worker)
    other_source = (project / read_manifest(other)["artifacts"]["source_bundle"]["path"]).read_bytes()
    assert other_source == original["source_bytes"]  # reproducible, so swap in a changed snapshot
    (project / "apps/panelbridge/panelbridge/models.py").write_text("changed\n")
    changed = source.build_bundle(project, "changed").path.read_bytes()
    with pytest.raises(ReleaseError, match="receipt"):
        inspect_release(**{**original, "source_bytes": changed})


def test_existing_output_outside_paths_and_root_are_refused(project, worker, monkeypatch, tmp_path):
    (project / "taken").mkdir()
    with pytest.raises(ReleaseError, match="new release output"):
        build_release(project, "taken", worker=worker)
    assert not (project / "taken" / "source").exists()
    for output in (tmp_path / "outside", project / ".." / "outside", project):
        with pytest.raises(ReleaseError):
            build_release(project, output, worker=worker)
    with pytest.raises(ReleaseError, match="absolute"):
        build_release(project, "release", worker=Path("worker"))
    monkeypatch.setattr(os, "getuid", lambda: 0)
    with pytest.raises(ReleaseError, match="unprivileged"):
        build_release(project, "release", worker=worker)
    assert not (project / "release").exists()


def test_package_stage_failure_stops_before_installer(project, worker, monkeypatch):
    monkeypatch.setattr(package, "_build_deb", lambda *args: (None, "dpkg_deb_unavailable", None))

    with pytest.raises(ReleaseError, match="dpkg_deb_unavailable"):
        build_release(project, "release", worker=worker)

    assert (project / "release" / "package").is_dir()
    assert not (project / "release" / "installer").exists()
    assert not (project / "release" / MANIFEST_NAME).exists()


def test_cli_reports_manifest_and_refuses_a_second_run(project):
    command = [sys.executable, "-I", str(APP / "packaging_tools/release.py"),
               "--project-root", str(project), "--output-dir", "release"]
    built = subprocess.run(command, capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    result = json.loads(built.stdout)
    assert result["stage"] == "source_bundle"
    assert result["release_ready"] is False
    assert result["manifest_sha256"] == digest(Path(result["manifest"]).read_bytes())
    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode == 2
    assert "new release output" in again.stderr
