"""Archive bytes and extraction checks use synthetic app trees, not a release."""

import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools.source_bundle import BundleError, SOURCE_FILES, build_bundle

APP = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    app = root / "apps/panelbridge"
    for relative in SOURCE_FILES:
        path = app / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if relative == "vendor/gnome-network-displays-0.99.0.tar.gz":
            path.write_bytes((APP / relative).read_bytes())
        elif relative == "native/wfd/build-worker.sh":
            path.write_bytes((APP / relative).read_bytes())
        elif relative.endswith(".png"):
            path.write_bytes(bytes.fromhex("89504e470d0a1a0a"))
        else:
            path.write_text("# Synthetic corresponding-source fixture\n", encoding="utf-8")
    return root


def digest(data):
    return hashlib.sha256(data).hexdigest()


def test_archive_extracts_with_exact_manifest_hashes_and_all_worker_inputs(project, tmp_path):
    result = build_bundle(project, project / "artifacts")
    archive_path = result.path
    assert archive_path.parent == project / "artifacts"
    assert result.sha256 == digest(archive_path.read_bytes())
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        assert all(member.isfile() and member.uid == member.gid == member.mtime == 0
                   and member.uname == member.gname == "" for member in members)
        assert all(member.mode in (0o644, 0o755) for member in members)
        archive.extractall(tmp_path / "unpacked", filter="data")
        manifest = json.load(archive.extractfile("panelbridge-source/SOURCE_MANIFEST.json"))
        names = {entry["path"] for entry in manifest["files"]}
        assert {
            "native/wfd/0001-adopt-managed-p2p.patch",
            "native/wfd/0002-explicit-profile.patch",
            "native/wfd/0003-runtime-metrics.patch",
            "native/wfd/0004-graceful-teardown.patch",
            "native/wfd/build-worker.sh", "native/wfd/panelbridge-wfd-worker.c",
            "native/wfd/pb-wfd-profile.c", "native/wfd/pb-wfd-profile.h",
            "native/wfd/pb-wfd-bridge.c", "native/wfd/pb-wfd-bridge.h",
            "native/wfd/pb-wfd-runtime.c", "native/wfd/pb-wfd-runtime.h",
            "native/wfd/pb-wfd-telemetry.c", "native/wfd/pb-wfd-telemetry.h",
            "LICENSE", "THIRD_PARTY_NOTICES.md", "UPSTREAM_PIN.json",
            "vendor/gnome-network-displays-0.99.0.tar.gz",
        } <= names
        assert len(members) == len(manifest["files"]) + 1
        for entry in manifest["files"]:
            data = (tmp_path / "unpacked/panelbridge-source" / entry["path"]).read_bytes()
            assert entry["sha256"] == digest(data)
            assert entry["size"] == len(data)
        pin = json.load(archive.extractfile("panelbridge-source/UPSTREAM_PIN.json"))
        assert pin["sha256"] == "b6314d25be7589c621b106c1712b00277c9b4d01d4796a78e987f10c0c1d1400"
        assert pin["version"] == "0.99.0"
        assert manifest["complete_binary_dependency_source"] is False
        assert manifest["distro_dependency_sources_included"] is False


def test_reproducible_across_source_metadata_output_name_and_umask(project):
    first = build_bundle(project, project / "first").path.read_bytes()
    for path in (project / "apps/panelbridge").rglob("*"):
        if path.is_file():
            path.chmod(0o700)
            os.utime(path, (12345, 12345))
    previous = os.umask(0o077)
    try:
        second = build_bundle(project, project / "second", name="another.tar.gz").path.read_bytes()
    finally:
        os.umask(previous)
    assert first == second
    assert first[:10] == bytes((31, 139, 8, 0, 0, 0, 0, 0, 2, 255))
    assert gzip.decompress(first) == gzip.decompress(second)


def test_expanded_vendor_caches_evidence_and_runtime_identity_are_excluded(project):
    for relative in (
        "apps/panelbridge/vendor/gnome-network-displays-0.99.0/private.txt",
        "apps/panelbridge/vendor/panelbridge-wfd-graceful-build/worker",
        "apps/panelbridge/panelbridge/__pycache__/state.pyc",
        "apps/panelbridge/.env", "apps/panelbridge/enrollment.json",
        "evidence/private.json", ".git/config",
    ):
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("EXCLUDED-PRIVATE-FIXTURE")
    result = build_bundle(project, project / "artifacts")
    with tarfile.open(result.path) as archive:
        for member in archive:
            assert b"EXCLUDED-PRIVATE-FIXTURE" not in archive.extractfile(member).read()


@pytest.mark.parametrize("missing", [
    "native/wfd/0004-graceful-teardown.patch", "native/wfd/pb-wfd-telemetry.h",
    "LICENSE", "assets/icons/hicolor/scalable/apps/org.panelbridge.PanelBridge.svg",
])
def test_missing_required_source_fails_without_artifact(project, missing):
    (project / "apps/panelbridge" / missing).unlink()
    with pytest.raises(BundleError, match="missing"):
        build_bundle(project, project / "artifacts")
    assert not (project / "artifacts").exists()


def test_changed_vendor_archive_or_recipe_pin_is_rejected(project):
    vendor = project / "apps/panelbridge/vendor/gnome-network-displays-0.99.0.tar.gz"
    original = vendor.read_bytes()
    vendor.write_bytes(original + b"tampered")
    with pytest.raises(BundleError, match="pin"):
        build_bundle(project, project / "artifacts")
    vendor.write_bytes(original)
    recipe = project / "apps/panelbridge/native/wfd/build-worker.sh"
    recipe.write_text(recipe.read_text().replace("expected=b631", "expected=c631"))
    with pytest.raises(BundleError, match="pin"):
        build_bundle(project, project / "artifacts")


@pytest.mark.parametrize("kind", ["file", "parent", "fifo", "hardlink"])
def test_nonregular_or_aliased_source_is_rejected(project, tmp_path, kind):
    app = project / "apps/panelbridge"
    source = app / "panelbridge/models.py"
    if kind == "parent":
        moved = app / "moved"
        (app / "panelbridge").rename(moved)
        (app / "panelbridge").symlink_to(moved, target_is_directory=True)
    else:
        source.unlink()
        if kind == "fifo":
            os.mkfifo(source)
        else:
            outside = tmp_path / "outside.py"
            outside.write_text("private source")
            if kind == "file":
                source.symlink_to(outside)
            else:
                os.link(outside, source)
    with pytest.raises(BundleError):
        build_bundle(project, project / "artifacts")
    assert not (project / "artifacts").exists()


@pytest.mark.parametrize("payload", [
    "/" + "Users" + "/" + "private-person" + "/project",
    "/" + "home" + "/" + "private-person" + "/project",
    "C:" + "\\Users\\" + "private-person\\project",
    ":".join(("a4", "b1", "c2", "d3", "e4", "f5")),
    "-".join(("a4", "b1", "c2", "d3", "e4", "f5")),
    "a4b1" + "." + "c2d3" + "." + "e4f5",
    "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
])
def test_private_text_fails_without_echoing_private_match(project, payload):
    (project / "apps/panelbridge/panelbridge/models.py").write_text(payload)
    with pytest.raises(BundleError) as raised:
        build_bundle(project, project / "artifacts")
    assert payload not in str(raised.value)
    assert not (project / "artifacts").exists()


def test_synthetic_mac_exception_is_limited_to_tests_and_known_fixture_range(project):
    address = ":".join(("02", "00", "00", "00", "00", "ff"))
    (project / "apps/panelbridge/tests/test_controller.py").write_text(address)
    build_bundle(project, project / "accepted")
    (project / "apps/panelbridge/panelbridge/models.py").write_text(address)
    with pytest.raises(BundleError, match="private"):
        build_bundle(project, project / "rejected")


def test_output_cannot_escape_project_follow_symlink_or_overwrite(project, tmp_path):
    result = build_bundle(project, project / "artifacts")
    before = result.path.read_bytes()
    with pytest.raises(BundleError, match="exist"):
        build_bundle(project, project / "artifacts")
    assert result.path.read_bytes() == before
    outside = tmp_path / "outside"
    outside.mkdir()
    (project / "linked-output").symlink_to(outside, target_is_directory=True)
    for output in (outside, project / "linked-output", project / ".." / "outside"):
        with pytest.raises(BundleError):
            build_bundle(project, output)
    assert list(outside.iterdir()) == []
    for name in ("../escape.tar.gz", "/escape.tar.gz", "bad\\escape.tar.gz", "plain.zip"):
        with pytest.raises(BundleError):
            build_bundle(project, project / "artifacts", name=name)


def test_source_content_changes_change_manifest_and_archive_hash(project):
    before = build_bundle(project, project / "before")
    source = project / "apps/panelbridge/panelbridge/models.py"
    source.write_text("# Changed synthetic implementation\n")
    after = build_bundle(project, project / "after")
    assert before.sha256 != after.sha256
    with tarfile.open(after.path) as archive:
        manifest = json.load(archive.extractfile("panelbridge-source/SOURCE_MANIFEST.json"))
        record = next(item for item in manifest["files"] if item["path"] == "panelbridge/models.py")
        assert record["sha256"] == digest(source.read_bytes())


def test_multicast_negative_fixture_exception_is_specific_to_its_test(project):
    address = ":".join(("03", "00", "00", "00", "00", "01"))
    (project / "apps/panelbridge/tests/test_rescue_service.py").write_text(address)
    build_bundle(project, project / "accepted")
    (project / "apps/panelbridge/tests/test_controller.py").write_text(address)
    with pytest.raises(BundleError, match="private"):
        build_bundle(project, project / "rejected")


def test_relative_output_is_anchored_to_project_not_process_working_directory(project, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = build_bundle(project, "artifacts")
    assert result.path.parent == project / "artifacts"
    assert not (tmp_path / "artifacts").exists()


def test_cli_reports_hash_and_refuses_existing_artifact(project):
    command = [sys.executable, str(APP / "packaging_tools/source_bundle.py"),
               "--project-root", str(project), "--output-dir", str(project / "artifacts")]
    built = subprocess.run(command, capture_output=True, text=True, check=False)
    assert built.returncode == 0, built.stderr
    result = json.loads(built.stdout)
    assert result["sha256"] == digest(Path(result["path"]).read_bytes())
    again = subprocess.run(command, capture_output=True, text=True, check=False)
    assert again.returncode != 0
    assert "exist" in again.stderr
