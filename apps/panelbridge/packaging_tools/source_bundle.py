"""Deterministic application/GND source archive; no distro dependency sources.

Only the exact reviewed file inventory below is eligible. Changes to the app's
file layout require an explicit inventory update before freezing a delivery.
"""

import argparse
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile


UPSTREAM_ARCHIVE = "vendor/gnome-network-displays-0.99.0.tar.gz"
UPSTREAM_SHA256 = "b6314d25be7589c621b106c1712b00277c9b4d01d4796a78e987f10c0c1d1400"
UPSTREAM_COMMIT = "eba3ce5dada5f6065e77041c973e73e3c2886bfd"
SOURCE_FILES = tuple(sorted("""
LICENSE
THIRD_PARTY_NOTICES.md
assets/icons/hicolor/128x128/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/16x16/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/24x24/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/256x256/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/32x32/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/48x48/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/512x512/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/64x64/apps/org.panelbridge.PanelBridge.png
assets/icons/hicolor/scalable/apps/org.panelbridge.PanelBridge.svg
helper/__init__.py
helper/advanced.py
helper/advanced_observer.py
helper/network.py
helper/service.py
interfaces/ADVANCED.md
interfaces/ADVANCED_OBSERVER.md
interfaces/CALIBRATION.md
interfaces/NETWORK_API.md
interfaces/RESCUE.md
interfaces/SESSION_API.md
interfaces/SOURCE_BUNDLE.md
native/wfd/0001-adopt-managed-p2p.patch
native/wfd/0002-explicit-profile.patch
native/wfd/0003-runtime-metrics.patch
native/wfd/0004-graceful-teardown.patch
native/wfd/build-worker.sh
native/wfd/panelbridge-wfd-worker.c
native/wfd/pb-wfd-bridge.c
native/wfd/pb-wfd-bridge.h
native/wfd/pb-wfd-frame-meta.c
native/wfd/pb-wfd-frame-meta.h
native/wfd/pb-wfd-profile.c
native/wfd/pb-wfd-profile.h
native/wfd/pb-wfd-runtime.c
native/wfd/pb-wfd-runtime.h
native/wfd/pb-wfd-telemetry.c
native/wfd/pb-wfd-telemetry.h
native/wfd/test-profile-gnd.c
native/wfd/test-profile.c
native/wfd/test-frame-meta.c
native/wfd/test-calibration.c
native/wfd/test-runtime.c
native/wfd/test-source-reset.c
packaging/helper-launch.py
packaging/debian/postinst
packaging/debian/prerm
packaging/debian/postrm
packaging/installer-bootstrap.py
packaging/maintain-launch.py
packaging/maintain-user-launch.py
packaging/org.panelbridge.Helper1.conf
packaging/oled-integration.json
packaging/org.panelbridge.PanelBridge.desktop
packaging/panelbridge
packaging/panelbridge-helper.service
packaging/panelbridge-rescue.service
packaging/panelbridge-session.desktop
packaging/rescue-launch.py
packaging/session-launch
packaging/session-launch.py
packaging/ui-launch.py
packaging_tools/__init__.py
packaging_tools/apt_plan.py
packaging_tools/build_package.py
packaging_tools/build_installer.py
packaging_tools/desktop_setup.py
packaging_tools/install_transaction.py
packaging_tools/maintenance.py
packaging_tools/render_icons.py
packaging_tools/runtime_access.py
packaging_tools/source_bundle.py
packaging_tools/user_setup.py
packaging_tools/user_maintenance.py
panelbridge/__init__.py
panelbridge/calibration.py
panelbridge/calibration_measurement.py
panelbridge/controller.py
panelbridge/credits.py
panelbridge/health.py
panelbridge/media.py
panelbridge/models.py
panelbridge/network_client.py
panelbridge/output.py
panelbridge/rescue.py
panelbridge/rescue_service.py
panelbridge/session_service.py
panelbridge/startup_choice.py
panelbridge/state.py
panelbridge/ui/__init__.py
panelbridge/ui/__main__.py
panelbridge/ui/app.py
panelbridge/ui/client.py
panelbridge/ui/presenter.py
tests/test_build_package.py
tests/test_advanced_helper.py
tests/test_advanced_observer.py
tests/test_build_installer.py
tests/test_apt_plan.py
tests/test_calibration.py
tests/test_calibration_measurement.py
tests/test_controller.py
tests/test_desktop_setup.py
tests/test_health.py
tests/test_install_transaction.py
tests/test_maintenance.py
tests/test_helper_authority.py
tests/test_media.py
tests/test_network_helper.py
tests/test_output.py
tests/test_rescue.py
tests/test_rescue_service.py
tests/test_runtime_access.py
tests/test_session_core.py
tests/test_session_service.py
tests/test_startup_choice.py
tests/test_source_bundle.py
tests/test_ui_presenter.py
tests/test_user_setup.py
tests/test_user_maintenance.py
tests/test_wfd_profile.py
tests/test_wfd_calibration_native.py
tests/test_wfd_runtime.py
tests/test_wfd_worker.py
vendor/gnome-network-displays-0.99.0.tar.gz
""".split()))
EXECUTABLE_FILES = frozenset({
    "native/wfd/build-worker.sh", "packaging/panelbridge", "packaging/session-launch",
    "packaging/helper-launch.py", "packaging/rescue-launch.py",
    "packaging/session-launch.py", "packaging/ui-launch.py",
    "packaging/debian/postinst", "packaging/debian/prerm", "packaging/debian/postrm",
})
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
PREFIX = "panelbridge-source/"
HOME_PATH = re.compile(r"(?i)(?:/(?:Users|home)/[^\s\"'<>/]+|[a-z]:[\\/]+Users[\\/]+[^\s\"'<>\\/]+)")
MAC = re.compile(r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4})(?![0-9a-f])")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")


class BundleError(RuntimeError):
    """Build refused; errors identify public filenames, not private matches."""


@dataclass(frozen=True)
class BundleResult:
    path: Path
    sha256: str
    file_count: int


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _json(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def _safe_parts(relative):
    path = PurePosixPath(relative)
    if (not relative or path.is_absolute() or "\\" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))
            or any(ord(char) < 32 for char in relative)):
        raise BundleError("Unsafe relative source path")
    return path.parts


def _directory(parent_fd, parts, *, create=False):
    current = os.dup(parent_fd)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
            if create and os.fstat(current).st_uid != os.getuid():
                raise BundleError("Output directory must be owned by the invoking user")
        return current
    except BaseException:
        os.close(current)
        raise


def _read_source(app_fd, relative):
    parts = _safe_parts(relative)
    parent_fd = _directory(app_fd, parts[:-1])
    try:
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise BundleError(f"Unexpected source type or hard link: {relative}")
            if before.st_size > MAX_FILE_BYTES:
                raise BundleError(f"Source exceeds size bound: {relative}")
            with os.fdopen(fd, "rb", closefd=False) as source:
                data = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or len(data) > MAX_FILE_BYTES
                    or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise BundleError(f"Source changed during read: {relative}")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _privacy_check(relative, data, *, upstream=False):
    text = data.decode("utf-8", "replace")
    if HOME_PATH.search(text) or PRIVATE_KEY.search(text):
        raise BundleError(f"Potential private material in: {relative}")
    for match in MAC.finditer(text):
        compact = re.sub(r"[:.\-]", "", match.group()).lower()
        # Exactly the reviewed synthetic test namespace, not all local MACs.
        synthetic = (not upstream and relative.startswith("tests/")
                     and compact.startswith("0200000000"))
        multicast_negative = (not upstream and relative == "tests/test_rescue_service.py"
                              and compact == "030000000001")
        if not (synthetic or multicast_negative):
            raise BundleError(f"Potential private address in: {relative}")


def _check_upstream(data):
    if _sha256(data) != UPSTREAM_SHA256:
        raise BundleError("Upstream archive pin mismatch")
    total = 0
    names = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for member in archive:
                parts = _safe_parts(member.name)
                if (parts[0] != "gnome-network-displays-0.99.0"
                        or member.name in names or len(names) >= 4096
                        or not (member.isfile() or member.isdir())):
                    raise BundleError("Unsafe pinned upstream archive member")
                names.add(member.name)
                total += member.size
                if member.size > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                    raise BundleError("Upstream archive exceeds size bounds")
                if member.isfile():
                    with archive.extractfile(member) as source:
                        _privacy_check(member.name, source.read(), upstream=True)
    except (tarfile.TarError, EOFError, OSError):
        raise BundleError("Invalid pinned upstream archive") from None


def _payload(app_fd):
    payload = {}
    total = 0
    for relative in SOURCE_FILES:
        try:
            data = _read_source(app_fd, relative)
        except FileNotFoundError:
            raise BundleError(f"Required source missing: {relative}") from None
        except OSError:
            raise BundleError(f"Unsafe or unreadable source: {relative}") from None
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise BundleError("Source snapshot exceeds total size bound")
        if relative == UPSTREAM_ARCHIVE:
            _check_upstream(data)
        elif relative.endswith(".png"):
            if not data.startswith(bytes.fromhex("89504e470d0a1a0a")):
                raise BundleError(f"Expected PNG source asset: {relative}")
            _privacy_check(relative, data)
        else:
            try:
                data.decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise BundleError(f"Expected UTF-8 text source: {relative}") from None
            if b"\0" in data:
                raise BundleError(f"Unexpected binary source: {relative}")
            _privacy_check(relative, data)
        payload[relative] = data
    recipe_pin = re.findall(rb"^expected=([0-9a-f]{64})$", payload["native/wfd/build-worker.sh"], re.M)
    if recipe_pin != [UPSTREAM_SHA256.encode("ascii")]:
        raise BundleError("Build recipe pin does not match the reviewed upstream archive")
    payload["UPSTREAM_PIN.json"] = _json({
        "name": "GNOME Network Displays", "version": "0.99.0", "commit": UPSTREAM_COMMIT,
        "archive": UPSTREAM_ARCHIVE, "sha256": UPSTREAM_SHA256,
        "build_recipe": "native/wfd/build-worker.sh", "recipe_pin_verified": True,
    })
    manifest = {
        "schema_version": 1,
        "scope": "PanelBridge application and pinned GNOME Network Displays source only",
        "complete_binary_dependency_source": False,
        "distro_dependency_sources_included": False,
        "dependency_distribution": "Distro dependencies are separately provided by APT.",
        "source_binary_match_verified": False,
        "files": [{"path": name, "sha256": _sha256(data), "size": len(data),
                   "mode": 0o755 if name in EXECUTABLE_FILES else 0o644}
                  for name, data in sorted(payload.items())],
    }
    payload["SOURCE_MANIFEST.json"] = _json(manifest)
    return payload


def _archive_bytes(payload):
    output = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=9) as zipped:
        with tarfile.open(fileobj=zipped, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            for name, data in sorted(payload.items()):
                _safe_parts(name)
                member = tarfile.TarInfo(PREFIX + name)
                member.size = len(data)
                member.mode = 0o755 if name in EXECUTABLE_FILES else 0o644
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def build_bundle(project_root, output_dir, *, name="panelbridge-app-source.tar.gz"):
    """Build under an owned project descendant; never replace or delete files."""
    project, output = Path(project_root), Path(output_dir)
    if ".." in project.parts or ".." in output.parts:
        raise BundleError("Path traversal is not allowed")
    project = project.absolute()
    output = output if output.is_absolute() else project / output
    try:
        output_parts = output.relative_to(project).parts
    except ValueError:
        raise BundleError("Output directory must be inside the project") from None
    if not output_parts:
        raise BundleError("Choose an output subdirectory inside the project")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\.tar\.gz", name):
        raise BundleError("Choose a simple .tar.gz filename")
    anchor = os.open(project.anchor, os.O_RDONLY | os.O_DIRECTORY)
    project_fd = app_fd = output_fd = None
    try:
        project_fd = _directory(anchor, project.parts[1:])
        if os.fstat(project_fd).st_uid != os.getuid():
            raise BundleError("Project must be owned by the invoking user")
        app_fd = _directory(project_fd, ("apps", "panelbridge"))
        payload = _payload(app_fd)
        content = _archive_bytes(payload)
        output_fd = _directory(project_fd, output_parts, create=True)
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=output_fd)
        except FileExistsError:
            raise BundleError("Output artifact already exists; choose a new filename") from None
        try:
            with os.fdopen(fd, "wb", closefd=False) as artifact:
                artifact.write(content)
                artifact.flush()
                os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(output_fd)
        return BundleResult(output / name, _sha256(content), len(payload))
    except OSError:
        raise BundleError("Filesystem operation failed; no existing file was replaced or deleted") from None
    finally:
        for fd in (output_fd, app_fd, project_fd, anchor):
            if fd is not None:
                os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="Owned project subdirectory; relative paths are relative to project root")
    parser.add_argument("--name", default="panelbridge-app-source.tar.gz")
    args = parser.parse_args(argv)
    try:
        result = build_bundle(args.project_root, args.output_dir, name=args.name)
    except BundleError as error:
        print(f"Source bundle refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"path": str(result.path), "sha256": result.sha256,
                      "file_count": result.file_count,
                      "complete_binary_dependency_source": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
