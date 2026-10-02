"""Build one deterministic experimental first-install .run without executing it.

The exact current source snapshot, ARM64 package and package receipt must agree.
The embedded bootstrap permits one bounded fresh-image install checkpoint.
Package hashes are integrity checks, not authentication or release validation.
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

from . import build_package as package
from . import source_bundle as source

MAX_DEB = 64 * 1024 * 1024
MAX_SOURCE = 16 * 1024 * 1024
MAX_RECEIPT = 1024 * 1024
MAX_ARTIFACT = 128 * 1024 * 1024
TOKEN = b"__PANELBRIDGE_PAYLOAD_B64__"
LIFECYCLE = {
    "usr/lib/panelbridge/maintain-launch.py": ("packaging/maintain-launch.py", 0o644),
    "usr/lib/panelbridge/packaging_tools/maintenance.py": ("packaging_tools/maintenance.py", 0o644),
    **{f"DEBIAN/{name}": (f"packaging/debian/{name}", 0o755)
       for name in ("postinst", "prerm", "postrm")},
}


class InstallerError(RuntimeError):
    """Installer assembly refused without changing existing files."""


@dataclass(frozen=True)
class InstallerResult:
    path: Path
    sha256: str
    lifecycle_inputs_present: bool
    ready_for_install: bool = True


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallerError("Duplicate receipt field")
        result[key] = value
    return result


def _read(path, limit):
    if not Path(path).is_absolute():
        raise InstallerError("Input paths must be absolute")
    return package._read_input(path, limit)


def _deb_files(data):
    """Read bounded archive bytes; the existing strict verifier checks structure."""
    if data[:8] != b"!<arch>\n":
        raise InstallerError("Input is not a Debian archive")
    offset, members = 8, {}
    while offset < len(data):
        header = data[offset:offset + 60]
        if len(header) != 60 or header[-2:] != b"`\n":
            raise InstallerError("Invalid Debian archive")
        name = header[:16].decode("ascii").strip().removesuffix("/")
        size = int(header[48:58].strip())
        offset += 60
        if name in members or size < 0 or offset + size > len(data):
            raise InstallerError("Invalid Debian archive bounds")
        members[name] = data[offset:offset + size]
        offset += size + size % 2
    if set(members) != {"debian-binary", "control.tar.gz", "data.tar.gz"}:
        raise InstallerError("Unexpected Debian archive members")
    result, total = {}, 0
    for name in ("control.tar.gz", "data.tar.gz"):
        with tarfile.open(fileobj=io.BytesIO(members[name]), mode="r:gz") as archive:
            count = 0
            for member in archive:
                count += 1
                if count > 512:
                    raise InstallerError("Debian archive has too many members")
                if member.isdir():
                    continue
                relative = member.name.removeprefix("./")
                source._safe_parts(relative)
                if name == "control.tar.gz":
                    relative = "DEBIAN/" + relative
                total += member.size
                if (not member.isfile() or member.size > MAX_DEB or total > package.MAX_PAYLOAD
                        or relative in result):
                    raise InstallerError("Unsafe Debian archive member")
                result[relative] = (archive.extractfile(member).read(), member.mode)
    return result


def _validate(deb, archive, raw_receipt, snapshot):
    receipt = json.loads(raw_receipt, object_pairs_hook=_unique)
    if (not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int
            or receipt["schema_version"] != 1 or receipt.get("package") != "panelbridge"
            or receipt.get("version") != package.VERSION or receipt.get("architecture") != "arm64"
            or receipt.get("deb_status") != "built" or receipt.get("ready_for_install") is not True
            or receipt.get("release_ready") is not False
            or receipt.get("source_snapshot_matches") is not True
            or receipt.get("deb_sha256") != _digest(deb)
            or receipt.get("source_sha256") != _digest(archive)
            or receipt.get("source_manifest_sha256") != _digest(snapshot["SOURCE_MANIFEST.json"])):
        raise InstallerError("Package receipt identity or hashes do not match")
    dependencies = receipt.get("runtime_dependencies")
    if (not isinstance(dependencies, list) or not 1 <= len(dependencies) <= 128
            or any(not isinstance(term, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9+.-]+(?: \((?:>=|<=|=|<<|>>) [A-Za-z0-9.+:~\-]+\))?", term)
                   for term in dependencies)
            or dependencies != sorted(set(dependencies))):
        raise InstallerError("Invalid runtime dependency inventory")
    names = {term.split()[0] for term in dependencies}
    if not {term.split()[0] for term in package.RUNTIME_DEPENDENCIES} <= names:
        raise InstallerError("Runtime dependency inventory is incomplete")
    actual = _deb_files(deb)
    worker = actual["usr/lib/panelbridge/bin/panelbridge-wfd-worker"][0]
    package._elf(worker)
    if _digest(worker) != receipt.get("worker_sha256"):
        raise InstallerError("Worker digest does not match")
    metadata = {key: value for key, value in receipt.items() if key not in ("deb_status", "deb_sha256", "payload_files")}
    expected = {destination: (snapshot[original], mode)
                for destination, (original, mode) in package.INSTALLED_FILES.items()}
    expected.update({
        "usr/lib/panelbridge/bin/panelbridge-wfd-worker": (worker, 0o755),
        "usr/share/panelbridge/panelbridge-app-source.tar.gz": (archive, 0o644),
        "usr/share/panelbridge/PACKAGE_PROVENANCE.json": (source._json(metadata), 0o644),
    })
    total = sum(len(data) for data, _ in expected.values())
    md5sums = "".join(f"{hashlib.md5(data, usedforsecurity=False).hexdigest()}  {path}\n"
                      for path, (data, _) in sorted(expected.items())).encode()
    expected["DEBIAN/md5sums"] = (md5sums, 0o644)
    expected["DEBIAN/control"] = (package._control(dependencies, (total + 1023) // 1024), 0o644)
    expected.update({destination: (snapshot[original], mode)
                     for destination, (original, mode) in package.CONTROL_FILES.items()})
    package._verify_deb(deb, expected)
    inventory = [{"path": path, "sha256": _digest(data), "size": len(data), "mode": mode,
                  "installed_uid": 0, "installed_gid": 0}
                 for path, (data, mode) in sorted(expected.items())]
    if receipt.get("payload_files") != inventory:
        raise InstallerError("Package receipt payload map does not match")
    present = all(original in snapshot and expected.get(destination) == (snapshot[original], mode)
                  for destination, (original, mode) in LIFECYCLE.items())
    return present


def build_installer(project_root, output_dir, *, deb, source_bundle, package_receipt):
    """Write one fresh executable inside an owned project directory; never clobber."""
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise InstallerError("Run the builder as an unprivileged user")
    if not Path(project_root).is_absolute():
        raise InstallerError("Project root must be absolute")
    if ".." in Path(project_root).parts or ".." in Path(output_dir).parts:
        raise InstallerError("Path traversal is forbidden")
    project = package._absolute(project_root)
    output = Path(output_dir)
    output = package._absolute(output if output.is_absolute() else project / output)
    try:
        parts = output.relative_to(project).parts
    except ValueError:
        raise InstallerError("Output must stay inside the project") from None
    if not parts:
        raise InstallerError("Output must be a project subdirectory")
    handles = []
    try:
        project_fd = package._open_directory(project)
        handles.append(project_fd)
        package._owned(project_fd)
        app_fd = source._directory(project_fd, ("apps", "panelbridge"))
        handles.append(app_fd)
        snapshot = source._payload(app_fd)
        for required in ("packaging/installer-bootstrap.py", "packaging_tools/build_installer.py", "packaging_tools/apt_plan.py"):
            if required not in snapshot:
                raise InstallerError("Installer sources are not in the canonical source inventory")
        raw_deb, raw_source, raw_receipt = _read(deb, MAX_DEB), _read(source_bundle, MAX_SOURCE), _read(package_receipt, MAX_RECEIPT)
        if raw_source != source._archive_bytes(snapshot):
            raise InstallerError("Source does not match the current canonical snapshot")
        present = _validate(raw_deb, raw_source, raw_receipt, snapshot)
        template = snapshot["packaging/installer-bootstrap.py"]
        if template.count(TOKEN) != 1 or not template.startswith(b"#!/usr/bin/python3 -I\n"):
            raise InstallerError("Unexpected bootstrap template")
        envelope = {"schema_version": 1, "files": {
            name: {"data": base64.b64encode(data).decode("ascii"), "sha256": _digest(data), "size": len(data)}
            for name, data in (("package.deb", raw_deb), ("source.tar.gz", raw_source), ("package-receipt.json", raw_receipt))}}
        content = template.replace(TOKEN, base64.b64encode(source._json(envelope)))
        if len(content) > MAX_ARTIFACT:
            raise InstallerError("Installer exceeds its size bound")
        compile(content, "panelbridge-installer.run", "exec")
        output_fd = package._artifact_directory(project_fd, parts)
        handles.append(output_fd)
        filename = f"panelbridge_{package.VERSION}_arm64_experimental.run"
        fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o755, dir_fd=output_fd)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o755)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.fsync(output_fd)
        return InstallerResult(output / filename, _digest(content), present)
    except InstallerError:
        raise
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, SyntaxError, RecursionError,
            package.PackageError, source.BundleError, tarfile.TarError, EOFError):
        raise InstallerError("Unsafe, inconsistent or changed input/output; existing files are preserved") from None
    finally:
        for fd in reversed(handles):
            os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--deb", type=Path, required=True)
    parser.add_argument("--source-bundle", type=Path, required=True)
    parser.add_argument("--package-receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_installer(args.project_root, args.output_dir, deb=args.deb,
                                 source_bundle=args.source_bundle, package_receipt=args.package_receipt)
    except InstallerError as error:
        print(f"Installer assembly refused: {error}", file=sys.stderr)
        return 2
    print(json.dumps({"path": str(result.path), "sha256": result.sha256,
                      "lifecycle_inputs_present": result.lifecycle_inputs_present,
                      "ready_for_install": result.ready_for_install, "release_ready": False,
                      "release_gate": "experimental_first_install"}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
