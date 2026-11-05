"""Assemble and inspect one experimental PanelBridge ARM64 release set.

The three existing builders already enforce their own contracts: the source
bundle is an exact allowlisted snapshot with the pinned upstream archive, the
package carries that snapshot beside the worker and the notices, and the
installer refuses unless package, receipt and snapshot agree. This module runs
them in that order inside one fresh owned output directory, reads every
artifact back from disk, cross-checks them against each other, and records the
hashes in RELEASE_MANIFEST.json.

Two things are deliberately outside its reach. It never marks a release ready:
physical Pi 500 acceptance is an owner observation recorded in docs/PI500.md,
not a property a build can establish. And it never touches the repository's
root Python wheel, which packages only the legacy MIT HCCAST driver; a
successful wheel build delivers nothing of the wireless product.

Without --worker only the platform-independent source bundle is built and
inspected. That is the software check CI can run on an x86 host. The ARM64
worker must be compiled on the target platform with native/wfd/build-worker.sh,
in a neutral build path, and stripped before it is accepted here.
"""

import argparse
import base64
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sys
import tarfile

if __package__:
    from . import build_installer as installer
    from . import build_package as package
    from . import source_bundle as source
else:  # Executed by path, for example: python3 -I apps/panelbridge/packaging_tools/release.py
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from packaging_tools import build_installer as installer
    from packaging_tools import build_package as package
    from packaging_tools import source_bundle as source

MANIFEST_NAME = "RELEASE_MANIFEST.json"
WORKER_PATH = "usr/lib/panelbridge/bin/panelbridge-wfd-worker"
SOURCE_PATH = "usr/share/panelbridge/panelbridge-app-source.tar.gz"
PROVENANCE_PATH = "usr/share/panelbridge/PACKAGE_PROVENANCE.json"
NOTICE_PATHS = {
    "usr/share/panelbridge/LICENSE": "LICENSE",
    "usr/share/panelbridge/THIRD_PARTY_NOTICES.md": "THIRD_PARTY_NOTICES.md",
    "usr/share/doc/panelbridge/copyright": "THIRD_PARTY_NOTICES.md",
}
# The modified sender is only complete as source when every one of these travels
# with the binary: the pinned upstream archive, its pin record, the four patches,
# the recipe that applies them and every PanelBridge native file.
PATCHED_SENDER_SOURCES = frozenset({
    "UPSTREAM_PIN.json", "SOURCE_MANIFEST.json", "LICENSE", "THIRD_PARTY_NOTICES.md",
    source.UPSTREAM_ARCHIVE,
    "native/wfd/0001-adopt-managed-p2p.patch", "native/wfd/0002-explicit-profile.patch",
    "native/wfd/0003-runtime-metrics.patch", "native/wfd/0004-graceful-teardown.patch",
    "native/wfd/build-worker.sh", "native/wfd/panelbridge-wfd-worker.c",
    "native/wfd/pb-wfd-bridge.c", "native/wfd/pb-wfd-bridge.h",
    "native/wfd/pb-wfd-frame-meta.c", "native/wfd/pb-wfd-frame-meta.h",
    "native/wfd/pb-wfd-profile.c", "native/wfd/pb-wfd-profile.h",
    "native/wfd/pb-wfd-runtime.c", "native/wfd/pb-wfd-runtime.h",
    "native/wfd/pb-wfd-telemetry.c", "native/wfd/pb-wfd-telemetry.h",
})
# Byte markers of a development build: the recipe's private staging directory,
# throwaway build roots, and unstripped debug records that embed build paths.
DEVELOPMENT_MARKERS = (
    b"panelbridge-wfd-graceful-build", b"/root/", b"/tmp/", b"/var/tmp/", b"/build/",
)
DEBUG_SECTION_MARKERS = (b".debug_info", b".debug_str", b".debug_line")
PAYLOAD_LINE = re.compile(rb'^PAYLOAD_B64 = "([A-Za-z0-9+/=]+)"$', re.M)
MAX_MANIFEST = 1024 * 1024


class ReleaseError(RuntimeError):
    """Release assembly refused; reasons are fixed text, never private content."""


@dataclass(frozen=True)
class ReleaseResult:
    output_dir: Path
    manifest_path: Path
    manifest_sha256: str
    stage: str
    release_ready: bool = False


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def inspect_worker(data):
    """Refuse a worker that is not a clean, stripped ARM64 build.

    The ELF and home-path checks are the package builder's own. On top of them a
    release worker may not reference the recipe's staging directory, throwaway
    build roots or carry debug records, which is where the development worker
    kept its private build paths.
    """
    try:
        package._elf(data)
    except (package.PackageError, source.BundleError) as error:
        raise ReleaseError(f"Worker rejected: {error}") from None
    if any(marker in data for marker in DEVELOPMENT_MARKERS):
        raise ReleaseError("Worker carries development build paths; rebuild in a neutral path")
    if any(marker in data for marker in DEBUG_SECTION_MARKERS):
        raise ReleaseError("Worker carries debug records; strip it before release")
    return _digest(data)


def _source_members(data):
    """Return {relative path: bytes} for a source bundle, refusing unsafe archives."""
    members = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                parts = source._safe_parts(member.name)
                if parts[0] != source.PREFIX.rstrip("/") or len(parts) < 2 or not member.isfile():
                    raise ReleaseError("Source bundle has an unexpected member")
                name = "/".join(parts[1:])
                if name in members or member.size > source.MAX_FILE_BYTES or len(members) >= 512:
                    raise ReleaseError("Source bundle has an unexpected member")
                members[name] = archive.extractfile(member).read()
    except (tarfile.TarError, EOFError, OSError, source.BundleError):
        raise ReleaseError("Source bundle is not a readable archive") from None
    return members


def inspect_source_bundle(data):
    """Check that the bundle carries the complete patched sender source and pin."""
    members = _source_members(data)
    missing = sorted(PATCHED_SENDER_SOURCES - set(members))
    if missing:
        raise ReleaseError("Source bundle lacks patched sender inputs: " + ", ".join(missing))
    if _digest(members[source.UPSTREAM_ARCHIVE]) != source.UPSTREAM_SHA256:
        raise ReleaseError("Bundled upstream archive does not match the reviewed pin")
    try:
        pin = json.loads(members["UPSTREAM_PIN.json"])
        manifest = json.loads(members["SOURCE_MANIFEST.json"])
    except ValueError:
        raise ReleaseError("Source bundle records are not valid JSON") from None
    if (not isinstance(pin, dict) or pin.get("sha256") != source.UPSTREAM_SHA256
            or pin.get("commit") != source.UPSTREAM_COMMIT or pin.get("recipe_pin_verified") is not True):
        raise ReleaseError("Upstream pin record does not match the reviewed pin")
    if not isinstance(manifest, dict) or manifest.get("source_binary_match_verified") is not False:
        raise ReleaseError("Source manifest must not claim a verified source-to-binary match")
    recorded = {entry.get("path"): entry.get("sha256") for entry in manifest.get("files", [])
                if isinstance(entry, dict)}
    for name, content in members.items():
        if name != "SOURCE_MANIFEST.json" and recorded.get(name) != _digest(content):
            raise ReleaseError("Source manifest digests do not match the archive contents")
    return {
        "sha256": _digest(data), "size": len(data), "file_count": len(members),
        "upstream": {"archive": source.UPSTREAM_ARCHIVE, "sha256": source.UPSTREAM_SHA256,
                     "commit": source.UPSTREAM_COMMIT},
        "notices": {name: _digest(members[name]) for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")},
    }


def _installer_payload(content):
    found = PAYLOAD_LINE.findall(content)
    if len(found) != 1:
        raise ReleaseError("Installer does not embed exactly one payload")
    try:
        envelope = json.loads(base64.b64decode(found[0], validate=True))
        files = envelope["files"]
        return {name: base64.b64decode(entry["data"], validate=True) for name, entry in files.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ReleaseError("Installer payload is not decodable") from None


def inspect_release(*, source_bytes, deb_bytes, receipt_bytes, installer_bytes):
    """Cross-check the four artifacts; return the manifest fragment they justify."""
    bundle = inspect_source_bundle(source_bytes)
    try:
        receipt = json.loads(receipt_bytes)
    except ValueError:
        raise ReleaseError("Package receipt is not valid JSON") from None
    if (not isinstance(receipt, dict) or receipt.get("version") != package.VERSION
            or receipt.get("architecture") != "arm64" or receipt.get("deb_status") != "built"
            or receipt.get("source_sha256") != bundle["sha256"]
            or receipt.get("deb_sha256") != _digest(deb_bytes)
            or receipt.get("release_ready") is not False
            or receipt.get("worker_source_match_verified") is not False):
        raise ReleaseError("Package receipt does not describe these artifacts")
    try:
        payload = installer._deb_files(deb_bytes)
    except (installer.InstallerError, source.BundleError, tarfile.TarError, EOFError, OSError):
        raise ReleaseError("Package is not a readable Debian archive") from None
    required = {WORKER_PATH, SOURCE_PATH, PROVENANCE_PATH, *NOTICE_PATHS}
    missing = sorted(required - set(payload))
    if missing:
        raise ReleaseError("Package lacks required members: " + ", ".join(missing))
    if payload[SOURCE_PATH][0] != source_bytes:
        raise ReleaseError("Package does not carry the matching source bundle")
    snapshot = _source_members(source_bytes)
    for installed, original in NOTICE_PATHS.items():
        if payload[installed][0] != snapshot[original]:
            raise ReleaseError(f"Installed notice differs from the bundled source: {installed}")
    worker_sha256 = inspect_worker(payload[WORKER_PATH][0])
    if receipt.get("worker_sha256") != worker_sha256:
        raise ReleaseError("Package receipt names a different worker")
    for path, (data, _mode) in payload.items():
        if path not in (WORKER_PATH, SOURCE_PATH):
            try:
                source._privacy_check(path, data)
            except source.BundleError:
                raise ReleaseError(f"Package member carries private text: {path}") from None
    embedded = _installer_payload(installer_bytes)
    expected = {"package.deb": deb_bytes, "source.tar.gz": source_bytes,
                "package-receipt.json": receipt_bytes}
    if embedded != expected:
        raise ReleaseError("Installer does not embed exactly these artifacts")
    try:
        provenance = json.loads(payload[PROVENANCE_PATH][0])
    except ValueError:
        raise ReleaseError("Package provenance record is not valid JSON") from None
    if not isinstance(provenance, dict) or provenance.get("release_ready") is not False:
        raise ReleaseError("Package provenance must not claim release readiness")
    return {
        "source_bundle": bundle,
        "package": {"sha256": _digest(deb_bytes), "size": len(deb_bytes),
                    "member_count": len(payload), "worker_sha256": worker_sha256,
                    "dependency_proof_status": receipt.get("dependency_proof_status")},
        "receipt": {"sha256": _digest(receipt_bytes)},
        "installer": {"sha256": _digest(installer_bytes), "size": len(installer_bytes)},
    }


def _claims(stage):
    return {
        "release_ready": False,
        "release_gate": "experimental_first_install",
        "physical_acceptance": "not_established_by_this_build; see docs/PI500.md",
        "software_verification": "artifact_cross_check" if stage == "installer" else "source_bundle_only",
        "legacy_wheel_delivers_panelbridge": False,
        "complete_binary_dependency_source": False,
    }


def _write_manifest(output, manifest):
    content = source._json(manifest)
    if len(content) > MAX_MANIFEST:
        raise ReleaseError("Release manifest exceeds its size bound")
    fd = os.open(output / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return _digest(content)


def build_release(project_root, output_dir, *, worker=None, shlibdeps=None):
    """Build source bundle, package and installer into a new owned directory.

    Returns a ReleaseResult. With ``worker`` omitted the stage is
    ``source_bundle`` and no package or installer is produced. Partial output
    from a refused later stage is preserved and reported; the manifest is only
    written after every produced artifact has been read back and cross-checked.
    """
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise ReleaseError("Run the release assembler as an unprivileged user, never root")
    if not Path(project_root).is_absolute():
        raise ReleaseError("Project root must be absolute")
    if ".." in Path(project_root).parts or ".." in Path(output_dir).parts:
        raise ReleaseError("Path traversal is forbidden")
    project = package._absolute(project_root)
    output = Path(output_dir)
    output = package._absolute(output if output.is_absolute() else project / output)
    try:
        parts = output.relative_to(project).parts
    except ValueError:
        raise ReleaseError("Output must stay inside the project") from None
    if not parts:
        raise ReleaseError("Output must be a project subdirectory")
    if os.path.lexists(output):
        raise ReleaseError("Choose a new release output directory; existing files are preserved")
    if worker is not None:
        # Refuse a development or unstripped worker before anything is written;
        # the same inspection is repeated on the packaged copy afterwards.
        if not Path(worker).is_absolute():
            raise ReleaseError("Worker path must be absolute")
        try:
            inspect_worker(package._read_input(worker))
        except package.PackageError as error:
            raise ReleaseError(f"Worker rejected: {error}") from None
    try:
        os.makedirs(output.parent, mode=0o700, exist_ok=True)
        os.mkdir(output, 0o700)
    except OSError:
        raise ReleaseError("Cannot create the release output directory") from None
    try:
        bundle = source.build_bundle(project, output / "source")
    except source.BundleError as error:
        raise ReleaseError(f"Source bundle refused: {error}") from None
    source_bytes = package._read_input(bundle.path, installer.MAX_SOURCE)
    if _digest(source_bytes) != bundle.sha256:
        raise ReleaseError("Source bundle changed after it was written")
    manifest = {
        "schema_version": 1, "package": "panelbridge", "version": package.VERSION,
        "architecture": "arm64", "stage": "source_bundle",
        "artifacts": {"source_bundle": {"path": str(bundle.path.relative_to(project)),
                                        **inspect_source_bundle(source_bytes)}},
        "claims": _claims("source_bundle"),
    }
    if worker is None:
        digest = _write_manifest(output, manifest)
        return ReleaseResult(output, output / MANIFEST_NAME, digest, "source_bundle")
    try:
        built = package.build_package(project, output / "package", source_bundle=bundle.path,
                                      worker=worker, shlibdeps=shlibdeps, build_deb=True)
    except package.PackageError as error:
        raise ReleaseError(f"Package refused: {error}") from None
    if built.deb_status != "built" or built.deb_path is None:
        raise ReleaseError(f"Package stage ended with status {built.deb_status}; no installer was built")
    try:
        run = installer.build_installer(project, output / "installer", deb=built.deb_path,
                                        source_bundle=bundle.path, package_receipt=built.receipt_path)
    except installer.InstallerError as error:
        raise ReleaseError(f"Installer refused: {error}") from None
    try:
        deb_bytes = package._read_input(built.deb_path, installer.MAX_DEB)
        receipt_bytes = package._read_input(built.receipt_path, installer.MAX_RECEIPT)
        installer_bytes = package._read_input(run.path, installer.MAX_ARTIFACT)
    except package.PackageError as error:
        raise ReleaseError(f"Artifact readback refused: {error}") from None
    inspection = inspect_release(source_bytes=source_bytes, deb_bytes=deb_bytes,
                                 receipt_bytes=receipt_bytes, installer_bytes=installer_bytes)
    if inspection["installer"]["sha256"] != run.sha256:
        raise ReleaseError("Installer changed after it was written")
    manifest["stage"] = "installer"
    manifest["artifacts"] = {
        "source_bundle": {"path": str(bundle.path.relative_to(project)), **inspection["source_bundle"]},
        "package": {"path": str(built.deb_path.relative_to(project)), **inspection["package"]},
        "receipt": {"path": str(built.receipt_path.relative_to(project)), **inspection["receipt"]},
        "installer": {"path": str(run.path.relative_to(project)), **inspection["installer"]},
    }
    manifest["claims"] = _claims("installer")
    digest = _write_manifest(output, manifest)
    return ReleaseResult(output, output / MANIFEST_NAME, digest, "installer")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="new project subdirectory; relative paths are relative to the project root")
    parser.add_argument("--worker", type=Path,
                        help="stripped ARM64 panelbridge-wfd-worker built on the target platform")
    parser.add_argument("--shlibdeps", type=Path, help="optional dpkg-shlibdeps JSON receipt")
    args = parser.parse_args(argv)
    try:
        result = build_release(args.project_root, args.output_dir, worker=args.worker,
                               shlibdeps=args.shlibdeps)
    except ReleaseError as error:
        print(f"Release assembly refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"output_dir": str(result.output_dir), "manifest": str(result.manifest_path),
                      "manifest_sha256": result.manifest_sha256, "stage": result.stage,
                      "release_ready": False, "release_gate": "experimental_first_install"},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
