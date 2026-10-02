"""Stage a stock-clock ARM64 experimental first-install package; never activate it.

build_package(project_root, output_dir, source_bundle=..., worker=...) accepts
the exact canonical archive from source_bundle.py for the current app snapshot.
An optional shlibdeps JSON receipt binds a successful dpkg-shlibdeps dependency
string to the exact worker hash; this is supplied evidence, not authentication.
No binary/source provenance, complete offline closure or successful installation
is inferred from ELF headers, source matching, or successful dpkg-deb execution.
"""

import argparse
from dataclasses import dataclass
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import sys
import tarfile

from . import source_bundle as source

VERSION = "0.1.0~dev3"
NAME = f"panelbridge_{VERSION}_arm64"
MAX_INPUT = 64 * 1024 * 1024
MAX_PAYLOAD = 128 * 1024 * 1024
BUILD_TIMEOUT = 120

# Explicit runtime inventory. No test/build tools or development headers.
PYTHON_FILES = """
helper/__init__.py helper/network.py helper/service.py
packaging_tools/__init__.py packaging_tools/desktop_setup.py packaging_tools/user_setup.py
packaging_tools/install_transaction.py packaging_tools/user_maintenance.py packaging_tools/apt_plan.py
packaging_tools/maintenance.py
packaging_tools/runtime_access.py
panelbridge/__init__.py panelbridge/calibration.py panelbridge/calibration_measurement.py
panelbridge/controller.py panelbridge/credits.py panelbridge/health.py panelbridge/media.py
panelbridge/models.py panelbridge/network_client.py panelbridge/output.py panelbridge/rescue.py
panelbridge/rescue_service.py panelbridge/session_service.py panelbridge/startup_choice.py panelbridge/state.py
panelbridge/ui/__init__.py panelbridge/ui/__main__.py panelbridge/ui/app.py
panelbridge/ui/client.py panelbridge/ui/presenter.py
""".split()
INSTALLED_FILES = {f"usr/lib/panelbridge/{name}": (name, 0o644) for name in PYTHON_FILES}
INSTALLED_FILES.update({
    "usr/bin/panelbridge": ("packaging/panelbridge", 0o755),
    "usr/lib/panelbridge/session-launch": ("packaging/session-launch", 0o755),
    **{f"usr/lib/panelbridge/{name}-launch.py": (f"packaging/{name}-launch.py", 0o644)
       for name in ("helper", "rescue", "session", "ui", "maintain-user", "maintain")},
    **{f"usr/lib/systemd/system/panelbridge-{name}.service": (
        f"packaging/panelbridge-{name}.service", 0o644) for name in ("helper", "rescue")},
    "usr/share/dbus-1/system.d/org.panelbridge.Helper1.conf": (
        "packaging/org.panelbridge.Helper1.conf", 0o644),
    "usr/share/applications/org.panelbridge.PanelBridge.desktop": (
        "packaging/org.panelbridge.PanelBridge.desktop", 0o644),
    "usr/share/panelbridge/panelbridge-session.desktop": (
        "packaging/panelbridge-session.desktop", 0o644),
    "usr/share/panelbridge/oled-integration.json": ("packaging/oled-integration.json", 0o644),
    **{f"usr/share/panelbridge/{name}": (name, 0o644)
       for name in ("LICENSE", "THIRD_PARTY_NOTICES.md")},
    "usr/share/doc/panelbridge/copyright": ("THIRD_PARTY_NOTICES.md", 0o644),
})
CONTROL_FILES = {f"DEBIAN/{name}": (f"packaging/debian/{name}", 0o755)
                 for name in ("postinst", "prerm", "postrm")}
for size in ("16x16", "24x24", "32x32", "48x48", "64x64", "128x128", "256x256", "512x512", "scalable"):
    suffix = "svg" if size == "scalable" else "png"
    path = f"icons/hicolor/{size}/apps/org.panelbridge.PanelBridge.{suffix}"
    INSTALLED_FILES[f"usr/share/{path}"] = (f"assets/{path}", 0o644)

# The first sixteen were observed in packaging-runtime-inspection.json. Versions
# record that dated inspection; Depends does not freeze security updates to them.
OBSERVED_RUNTIME_PACKAGES = {
    "gir1.2-gtk-4.0": "4.18.6+ds-2", "gstreamer1.0-libav": "1.26.2-1+deb13u1",
    "gstreamer1.0-plugins-bad": "1.26.2-3+rpt3+deb13u2",
    "gstreamer1.0-plugins-base": "1.26.2-1+rpt3+deb13u1",
    "gstreamer1.0-plugins-good": "1.26.2-1+deb13u2",
    "gstreamer1.0-plugins-ugly": "1.26.3-4+deb13u1",
    "gstreamer1.0-tools": "1.26.2-2", "kanshi": "1.5.1-2+b1",
    "libgstrtspserver-1.0-0": "1.26.2-1", "network-manager": "1.52.1-1+rpt4",
    "python3": "3.13.5-1", "python3-cairo": "1.27.0-2", "python3-gi": "3.50.0-4+b1",
    "raspi-utils": "20260626-1", "wf-recorder": "0.5.0-2", "wlr-randr": "0.4.1-1",
}
RUNTIME_DEPENDENCIES = tuple(sorted({
    *(name for name in OBSERVED_RUNTIME_PACKAGES if name != "python3"),
    "python3 (>= 3.13)", "dbus", "systemd", "labwc", "iproute2", "iw",
    "libglib2.0-0t64", "libgstreamer1.0-0", "libgstreamer-plugins-base1.0-0",
    "libgtk-4-1", "libnm0", "libpulse0", "libavahi-common3", "libavahi-gobject0",
    "libjson-glib-1.0-0", "libprotobuf-c1", "libsoup-3.0-0",
}))


class PackageError(RuntimeError):
    """Build refused without exposing private input paths or payload matches."""


@dataclass(frozen=True)
class PackageResult:
    payload_dir: Path
    receipt_path: Path
    deb_path: Path | None
    deb_status: str


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _absolute(path):
    value = Path(path)
    if ".." in value.parts:
        raise PackageError("Path traversal is forbidden")
    return value.absolute()


def _open_directory(path):
    anchor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        return source._directory(anchor, path.parts[1:])
    finally:
        os.close(anchor)


def _owned(fd):
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise PackageError("Project/artifact directory must be owned and writable only by the caller")


def _artifact_directory(project_fd, parts):
    current = os.dup(project_fd)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
            _owned(current)
        return current
    except BaseException:
        os.close(current)
        raise


def _read_input(path, limit=MAX_INPUT):
    path = _absolute(path)
    parent = _open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_mode & 0o7000 or before.st_size > limit):
                raise PackageError("Input must be a bounded regular file without hard links or special modes")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or len(data) > limit
                    or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                raise PackageError("Input changed during validation")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _elf(data):
    if (len(data) < 64 or data[:7] != b"\x7fELF\x02\x01\x01"):
        raise PackageError("Worker must be an ELF64 little-endian AArch64 executable")
    fields = struct.unpack_from("<HHIQQQIHHHHHH", data, 16)
    kind, machine, version, entry, phoff, _, _, ehsize, phsize, phcount, _, _, _ = fields
    if (kind not in (2, 3) or machine != 183 or version != 1 or not entry
            or ehsize != 64 or phsize != 56 or not 1 <= phcount <= 256
            or phoff < 64 or phoff + phsize * phcount > len(data)):
        raise PackageError("Invalid ELF64 AArch64 executable header")
    executable_entry = False
    for index in range(phcount):
        segment, flags, offset, address, _, size, memory, _ = struct.unpack_from(
            "<IIQQQQQQ", data, phoff + index * phsize)
        if offset + size > len(data) or (segment == 1 and memory < size):
            raise PackageError("Invalid ELF segment bounds")
        if segment == 1 and flags & 1 and address <= entry < address + size:
            executable_entry = True
    if not executable_entry:
        raise PackageError("ELF entry is outside executable file-backed segments")
    source._privacy_check("native-worker", data)


def _dependencies(path, worker_hash):
    dependencies = set(RUNTIME_DEPENDENCIES)
    if path is None:
        return sorted(dependencies), "incomplete", None
    raw = _read_input(path, 65536)
    source._privacy_check("shlibdeps-receipt", raw)
    try:
        proof = json.loads(raw)
        if (not isinstance(proof, dict) or set(proof) != {
                "schema_version", "tool", "architecture", "exit_code", "worker_sha256", "depends"}
                or type(proof["schema_version"]) is not int or proof["schema_version"] != 1
                or type(proof["exit_code"]) is not int or proof["exit_code"] != 0
                or proof["tool"] != "dpkg-shlibdeps" or proof["architecture"] != "arm64"
                or proof["worker_sha256"] != worker_hash
                or not isinstance(proof["depends"], str) or not 1 <= len(proof["depends"]) <= 8192):
            raise ValueError
        terms = proof["depends"].split(",")
        if len(terms) > 128:
            raise ValueError
        for term in terms:
            term = term.strip()
            if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+(?: \((?:>=|<=|=|<<|>>) [A-Za-z0-9.+:~\-]+\))?", term):
                raise ValueError
            if " " in term:
                dependencies.discard(term.split()[0])
            dependencies.add(term)
    except (ValueError, KeyError, TypeError, RecursionError):
        raise PackageError("Invalid shlibdeps proof or worker hash mismatch") from None
    return sorted(dependencies), "native_shlibdeps_supplied", _digest(raw)


def _control(dependencies, installed_size):
    return (f"Package: panelbridge\nVersion: {VERSION}\nArchitecture: arm64\n"
            "Section: x11\nPriority: optional\nMaintainer: PanelBridge contributors <noreply@example.invalid>\n"
            f"Installed-Size: {installed_size}\nDepends: {', '.join(dependencies)}\n"
            "Description: PanelBridge experimental wireless display payload\n"
            " Stock-clock Raspberry Pi desktop and independent recovery components.\n"
            " Includes transactional enrollment and guarded removal hooks.\n"
            " Experimental first-install checkpoint; physical lifecycle validation remains incomplete.\n").encode()


def _stage(root_fd, payload):
    directories = set()
    for relative, (data, mode) in sorted(payload.items()):
        parts = source._safe_parts(relative)
        parent = os.dup(root_fd)
        try:
            for index, part in enumerate(parts[:-1]):
                directory = "/".join(parts[:index + 1])
                if directory not in directories:
                    os.mkdir(part, 0o755, dir_fd=parent)
                    directories.add(directory)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.fchmod(child, 0o755)
                os.close(parent)
                parent = child
            fd = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
            try:
                os.fchmod(fd, mode)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(fd)
                os.utime(fd, ns=(0, 0))
            finally:
                os.close(fd)
        finally:
            os.close(parent)
    for directory in sorted(directories, key=lambda name: name.count("/"), reverse=True):
        fd = source._directory(root_fd, directory.split("/"))
        try:
            os.utime(fd, ns=(0, 0))
            os.fsync(fd)
        finally:
            os.close(fd)
    os.fchmod(root_fd, 0o755)
    os.utime(root_fd, ns=(0, 0))
    os.fsync(root_fd)


def _verify_deb(data, payload):
    """Inspect dpkg output without extracting paths or executing its contents."""
    if data[:8] != b"!<arch>\n":
        raise PackageError("dpkg-deb did not create a Debian archive")
    members, offset = {}, 8
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[-2:] != b"`\n":
            raise PackageError("Invalid Debian archive header")
        try:
            size = int(header[48:58].strip())
            name = header[:16].decode("ascii").strip().removesuffix("/")
        except (ValueError, UnicodeError):
            raise PackageError("Invalid Debian archive member") from None
        offset += 60
        if size < 0 or offset + size > len(data) or name in members:
            raise PackageError("Invalid Debian archive bounds")
        members[name] = data[offset:offset + size]
        offset += size + size % 2
    if set(members) != {"debian-binary", "control.tar.gz", "data.tar.gz"} or members["debian-binary"] != b"2.0\n":
        raise PackageError("Unexpected Debian archive structure")
    actual = {}
    for name in ("control.tar.gz", "data.tar.gz"):
        total = 0
        names = set()
        expected = {path for path in payload
                    if path.startswith("DEBIAN/") == (name == "control.tar.gz")}
        observed = set()
        directories = {"/".join(path.split("/")[:index])
                       for path in payload if not path.startswith("DEBIAN/")
                       for index in range(1, len(path.split("/")))} if name == "data.tar.gz" else set()
        with tarfile.open(fileobj=io.BytesIO(members[name]), mode="r:gz") as archive:
            for member in archive:
                relative = member.name.removeprefix("./")
                root = member.isdir() and relative in ("", ".")
                if not root:
                    source._safe_parts(relative.rstrip("/"))
                if relative in names or len(names) > len(payload) + len(directories) + 1:
                    raise PackageError("Duplicate or excessive Debian archive members")
                names.add(relative)
                if member.uid != 0 or member.gid != 0 or member.mtime != 0:
                    raise PackageError("Debian archive ownership or time is not normalized")
                if member.isdir():
                    if member.mode != 0o755 or (not root and relative.rstrip("/") not in directories):
                        raise PackageError("Unexpected Debian directory or mode")
                    continue
                total += member.size
                if not member.isfile() or total > MAX_PAYLOAD or member.size > MAX_INPUT:
                    raise PackageError("Unsafe Debian payload member")
                relative = "DEBIAN/" + relative if name == "control.tar.gz" else relative
                if relative in actual or relative not in expected:
                    raise PackageError("Unexpected Debian payload path")
                observed.add(relative)
                actual[relative] = (archive.extractfile(member).read(), member.mode)
        if observed != expected:
            raise PackageError("Debian archive members are in the wrong section or missing")
    if actual != payload:
        raise PackageError("Debian archive does not match the staged payload")


def _build_deb(build_dir, payload_dir, payload, enabled):
    if not enabled:
        return None, "not_requested", None
    executable = shutil.which("dpkg-deb")
    if executable is None:
        return None, "dpkg_deb_unavailable", None
    path = build_dir / f"{NAME}.deb"
    args = [executable, "--root-owner-group", "--build", "--uniform-compression", "-Zgzip", "-z9",
            str(payload_dir), str(path)]
    try:
        outcome = subprocess.run(args, check=False, capture_output=True, timeout=BUILD_TIMEOUT,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C.UTF-8",
                                      "SOURCE_DATE_EPOCH": "0", "TZ": "UTC", "DPKG_DEB_THREADS_MAX": "1"})
        if outcome.returncode != 0:
            return None, "dpkg_deb_failed", None
        data = _read_input(path, MAX_PAYLOAD)
        _verify_deb(data, payload)
    except (OSError, subprocess.SubprocessError, PackageError, source.BundleError, tarfile.TarError, EOFError):
        return None, "dpkg_deb_failed", None
    return path, "built", _digest(data)


def build_package(project_root, output_dir, *, source_bundle, worker, shlibdeps=None, build_deb=True):
    """Create a fresh artifact subdirectory; validation failures precede staging.

    Optional shlibdeps JSON keys: schema_version=1, tool='dpkg-shlibdeps',
    architecture='arm64', exit_code=0, worker_sha256, depends (comma-separated
    simple Debian dependency relations). No live shlibdeps/ldd or install runs.
    """
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise PackageError("Run the package assembler as an unprivileged user, never root")
    if type(build_deb) is not bool:
        raise PackageError("build_deb must be boolean")
    project = _absolute(project_root)
    output = Path(output_dir)
    output = _absolute(output if output.is_absolute() else project / output)
    try:
        parts = output.relative_to(project).parts
    except ValueError:
        raise PackageError("Output must be a project-owned artifact subdirectory") from None
    if not parts:
        raise PackageError("Select an output subdirectory inside the project")
    handles = []
    try:
        project_fd = _open_directory(project)
        handles.append(project_fd)
        _owned(project_fd)
        app_fd = source._directory(project_fd, ("apps", "panelbridge"))
        handles.append(app_fd)
        snapshot = source._payload(app_fd)
        archive = _read_input(source_bundle)
        if archive != source._archive_bytes(snapshot):
            raise PackageError("Source bundle does not match the current validated source snapshot/manifest")
        worker_data = _read_input(worker)
        _elf(worker_data)
        dependencies, dependency_status, proof_hash = _dependencies(shlibdeps, _digest(worker_data))
        metadata = {
            "schema_version": 1, "package": "panelbridge", "version": VERSION, "architecture": "arm64",
            "scope": "stock-clock experimental first-install payload with guarded lifecycle hooks; installation unverified",
            "source_sha256": _digest(archive), "source_manifest_sha256": _digest(snapshot["SOURCE_MANIFEST.json"]),
            "source_snapshot_matches": True, "worker_sha256": _digest(worker_data),
            "worker_source_match_verified": False, "complete_binary_dependency_source": False,
            "dependency_proof_status": dependency_status, "shlibdeps_receipt_sha256": proof_hash,
            "runtime_dependencies": dependencies, "observed_runtime_versions": OBSERVED_RUNTIME_PACKAGES,
            "distro_dependencies_bundled": False, "offline_dependency_closure_verified": False,
            "supported_os_validation": "not_performed", "ready_for_install": True,
            "release_ready": False,
        }
        payload = {destination: (snapshot[original], mode)
                   for destination, (original, mode) in INSTALLED_FILES.items()}
        payload.update({
            "usr/lib/panelbridge/bin/panelbridge-wfd-worker": (worker_data, 0o755),
            "usr/share/panelbridge/panelbridge-app-source.tar.gz": (archive, 0o644),
            "usr/share/panelbridge/PACKAGE_PROVENANCE.json": (source._json(metadata), 0o644),
        })
        total = sum(len(data) for data, _ in payload.values())
        if total > MAX_PAYLOAD:
            raise PackageError("Payload exceeds the bounded package size")
        md5sums = "".join(f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {path}\n"
                          for path, (data, _) in sorted(payload.items())).encode()
        payload["DEBIAN/md5sums"] = (md5sums, 0o644)
        payload["DEBIAN/control"] = (_control(dependencies, (total + 1023) // 1024), 0o644)
        payload.update({destination: (snapshot[original], mode)
                        for destination, (original, mode) in CONTROL_FILES.items()})
        output_fd = _artifact_directory(project_fd, parts)
        handles.append(output_fd)
        _owned(output_fd)
        try:
            os.mkdir(NAME, 0o700, dir_fd=output_fd)
        except FileExistsError:
            raise PackageError("Artifact directory already exists; select a new output directory") from None
        build_fd = source._directory(output_fd, (NAME,))
        handles.append(build_fd)
        os.fchmod(build_fd, 0o700)
        os.mkdir("payload", 0o755, dir_fd=build_fd)
        payload_fd = source._directory(build_fd, ("payload",))
        handles.append(payload_fd)
        _stage(payload_fd, payload)
        build_dir = output / NAME
        payload_dir = build_dir / "payload"
        deb_path, deb_status, deb_hash = _build_deb(build_dir, payload_dir, payload, build_deb)
        receipt = {**metadata, "deb_status": deb_status, "deb_sha256": deb_hash,
                   "ready_for_install": deb_status == "built",
                   "payload_files": [{"path": path, "sha256": _digest(data), "size": len(data),
                                      "mode": mode, "installed_uid": 0, "installed_gid": 0}
                                     for path, (data, mode) in sorted(payload.items())]}
        receipt_path = build_dir / "package-receipt.json"
        fd = os.open(receipt_path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=build_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(source._json(receipt))
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(build_fd)
        os.fsync(output_fd)
        return PackageResult(payload_dir, receipt_path, deb_path, deb_status)
    except source.BundleError as error:
        raise PackageError(str(error)) from None
    except (OSError, KeyError):
        raise PackageError("Unsafe, missing or changed input/output; any new partial artifact is preserved") from None
    finally:
        for fd in reversed(handles):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--shlibdeps", type=Path)
    parser.add_argument("--payload-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = build_package(args.project_root, args.output_dir, source_bundle=args.source_bundle,
                               worker=args.worker, shlibdeps=args.shlibdeps, build_deb=not args.payload_only)
    except PackageError as error:
        print(f"Package assembly refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"payload_dir": str(result.payload_dir), "receipt_path": str(result.receipt_path),
                      "deb_path": str(result.deb_path) if result.deb_path else None,
                      "deb_status": result.deb_status, "ready_for_install": result.deb_status == "built",
                      "release_ready": False}, sort_keys=True))
    return 1 if result.deb_status == "dpkg_deb_failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
