#!/usr/bin/python3 -I
"""Self-contained experimental first-install installer. Hashes prove integrity, not authorship.

The fixed first-install gate permits a bounded fresh-image checkpoint.
There is no command-line or environment bypass. The dormant execution engine
uses only trusted staging and installed code; tests replace its OS boundaries.
"""

import base64
import fcntl
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import pwd
import re
import selectors
import signal
import stat
import subprocess
import sys
import tarfile
import time

# Compile-time gate for the approved experimental fresh-image attempt. This
# does not assert that installation, physical output or release is validated.
# Keep this fixed in the bundled source; no CLI or environment override exists.
EXPERIMENTAL_INSTALL_ENABLED = True
PAYLOAD_B64 = "__PANELBRIDGE_PAYLOAD_B64__"
MAX_ARTIFACT = 128 * 1024 * 1024
MAX_DEB = 64 * 1024 * 1024
MAX_SOURCE = 16 * 1024 * 1024
MAX_RECEIPT = 1024 * 1024
STAGING = Path("/var/lib/panelbridge/installer-staging")
LIFECYCLE = {
    "usr/lib/panelbridge/maintain-launch.py": ("packaging/maintain-launch.py", 0o644),
    "usr/lib/panelbridge/packaging_tools/maintenance.py": ("packaging_tools/maintenance.py", 0o644),
    **{f"DEBIAN/{name}": (f"packaging/debian/{name}", 0o755)
       for name in ("postinst", "prerm", "postrm")},
}
SUCCESS = {"api_version": 1, "action": "configure", "state": "awaiting_desktop_restart",
           "scope": "configuration_bytes_only", "live_output_restored": None}

# This is the entire first privileged program. It imports only the isolated
# interpreter's standard library; user-supplied paths are data, never imports.
ROOT_LOADER = r'''
import hashlib, os, pwd, re, secrets, stat, sys
try:
    if os.getuid() != 0 or os.geteuid() != 0 or len(sys.argv) != 3:
        raise ValueError()
    uid, gid = (os.environ.get(k, "") for k in ("SUDO_UID", "SUDO_GID"))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", uid) or not re.fullmatch(r"[1-9][0-9]{0,9}", gid):
        raise ValueError()
    account = pwd.getpwuid(int(uid))
    if (not 1000 <= int(uid) < 2**31 or account.pw_uid != int(uid)
            or type(account.pw_gid) is not int or not 0 < account.pw_gid < 2**31
            or account.pw_gid != int(gid) or account.pw_name != os.environ.get("SUDO_USER")
            or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,63}", account.pw_name)
            or not isinstance(account.pw_dir, str) or not account.pw_dir.startswith("/")
            or account.pw_dir == "/" or "\0" in account.pw_dir
            or any(p in (".", "..") for p in account.pw_dir.split("/"))):
        raise ValueError()
    named = pwd.getpwnam(account.pw_name)
    if (named.pw_uid, named.pw_gid, named.pw_dir) != (account.pw_uid, account.pw_gid, account.pw_dir):
        raise ValueError()
    home = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in filter(None, account.pw_dir.split("/")):
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
            os.close(home)
            home = child
        info = os.fstat(home)
        if info.st_uid != int(uid) or info.st_mode & 0o022:
            raise ValueError()
    finally:
        os.close(home)
    path, expected = sys.argv[1:]
    if not os.path.isabs(path) or len(path) > 4096 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != int(uid)
                or before.st_nlink != 1 or before.st_mode & 0o7000
                or not 0 < before.st_size <= 134217728):
            raise ValueError()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(134217729)
        after = os.fstat(fd)
        if (len(data) != before.st_size or hashlib.sha256(data).hexdigest() != expected
                or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ValueError()
    finally:
        os.close(fd)
    parent = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    for part in ("var", "lib", "panelbridge", "installer-staging"):
        if part in ("panelbridge", "installer-staging"):
            try:
                os.mkdir(part, 0o700, dir_fd=parent)
                os.fsync(parent)
            except FileExistsError:
                pass
        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        os.close(parent)
        parent = child
        info = os.fstat(parent)
        if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
            raise ValueError()
        if part in ("panelbridge", "installer-staging") and stat.S_IMODE(info.st_mode) != 0o700:
            raise ValueError()
    name = secrets.token_hex(16)
    os.mkdir(name, 0o700, dir_fd=parent)
    os.fsync(parent)
    stage = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    os.close(parent)
    for target, content in (("installer.run", data), ("artifact.sha256", expected.encode())):
        target_fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=stage)
        with os.fdopen(target_fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
    os.fsync(stage)
    os.close(stage)
    installed = "/var/lib/panelbridge/installer-staging/" + name + "/installer.run"
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/root",
           "SUDO_UID": uid, "SUDO_GID": gid, "SUDO_USER": account.pw_name}
    os.execve("/usr/bin/python3", ["/usr/bin/python3", "-I", "-B", installed, "--root-staged"], env)
except Exception:
    print('{"state":"staging_refused","ready_for_install":false}')
    sys.exit(2)
'''


class InstallRefused(RuntimeError):
    """Only fixed public reasons, never paths or command output."""


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallRefused("duplicate_json_field")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise InstallRefused("invalid_json") from None


def _safe_path(value):
    if (not isinstance(value, str) or len(value) > 256 or "\\" in value
            or any(part in ("", ".", "..") for part in value.split("/"))
            or not re.fullmatch(r"[A-Za-z0-9_./+~-]+", value)):
        raise InstallRefused("invalid_payload_path")
    return PurePosixPath(value).parts


def _source_files(data):
    result, total = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for entry in archive:
                parts = _safe_path(entry.name)
                name = "/".join(parts[1:])
                if (parts[0] != "panelbridge-source" or not name or not entry.isfile()
                        or name in result or len(result) >= 256 or entry.size > 16 * 1024 * 1024):
                    raise InstallRefused("invalid_source_archive")
                total += entry.size
                if total > 64 * 1024 * 1024:
                    raise InstallRefused("source_archive_too_large")
                result[name] = archive.extractfile(entry).read()
    except (tarfile.TarError, EOFError, OSError):
        raise InstallRefused("invalid_source_archive") from None
    return result


def _bundle(encoded=None):
    try:
        encoded = PAYLOAD_B64 if encoded is None else encoded
        if not isinstance(encoded, str) or len(encoded) > MAX_ARTIFACT:
            raise ValueError
        envelope = _json(base64.b64decode(encoded, validate=True))
        if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "files"} or envelope["schema_version"] != 1:
            raise ValueError
        files = envelope["files"]
        limits = {"package.deb": MAX_DEB, "source.tar.gz": MAX_SOURCE, "package-receipt.json": MAX_RECEIPT}
        if not isinstance(files, dict) or set(files) != set(limits):
            raise ValueError
        decoded = {}
        for name, limit in limits.items():
            entry = files[name]
            if not isinstance(entry, dict) or set(entry) != {"data", "sha256", "size"}:
                raise ValueError
            if type(entry["size"]) is not int or not 0 < entry["size"] <= limit:
                raise ValueError
            raw = base64.b64decode(entry["data"], validate=True)
            if len(raw) != entry["size"] or _digest(raw) != entry["sha256"]:
                raise ValueError
            decoded[name] = raw
        receipt = _json(decoded["package-receipt.json"])
        if (not isinstance(receipt, dict) or receipt.get("package") != "panelbridge"
                or receipt.get("architecture") != "arm64" or receipt.get("schema_version") != 1
                or receipt.get("deb_status") != "built" or receipt.get("ready_for_install") is not True
                or receipt.get("release_ready") is not False
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]{0,255}", receipt.get("version", ""))
                or receipt.get("deb_sha256") != _digest(decoded["package.deb"])
                or receipt.get("source_sha256") != _digest(decoded["source.tar.gz"])):
            raise ValueError
        entries = receipt.get("payload_files")
        if not isinstance(entries, list) or not 1 <= len(entries) <= 256:
            raise ValueError
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mode", "installed_uid", "installed_gid"}:
                raise ValueError
            _safe_path(entry["path"])
            if (entry["path"] in seen or entry["installed_uid"] != 0 or entry["installed_gid"] != 0
                    or type(entry["mode"]) is not int or entry["mode"] not in (0o644, 0o755)
                    or type(entry["size"]) is not int or not 0 <= entry["size"] <= MAX_DEB
                    or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
                raise ValueError
            seen.add(entry["path"])
        snapshot = _source_files(decoded["source.tar.gz"])
        if (_digest(snapshot.get("SOURCE_MANIFEST.json", b"")) != receipt.get("source_manifest_sha256")
                or "packaging_tools/apt_plan.py" not in snapshot):
            raise ValueError
        return decoded, receipt, snapshot
    except (ValueError, TypeError, KeyError, AttributeError):
        raise InstallRefused("artifact_integrity_failed") from None


def _lifecycle_present(receipt, snapshot):
    entries = {entry["path"]: entry for entry in receipt["payload_files"]}
    return all(original in snapshot and target in entries
               and entries[target]["sha256"] == _digest(snapshot[original])
               and entries[target]["mode"] == mode
               for target, (original, mode) in LIFECYCLE.items())


def _normal_account(uid):
    """Match maintenance's passwd/home predicate at every entry boundary."""
    try:
        if type(uid) is not int or not 1000 <= uid < 2**31:
            raise ValueError
        account = pwd.getpwuid(uid)
        if (account.pw_uid != uid or type(account.pw_gid) is not int or not 0 < account.pw_gid < 2**31
                or not re.fullmatch(r"[a-z_][a-z0-9_.-]{0,63}", account.pw_name)
                or not isinstance(account.pw_dir, str) or not account.pw_dir.startswith("/")
                or account.pw_dir == "/" or "\0" in account.pw_dir
                or any(p in (".", "..") for p in account.pw_dir.split("/"))):
            raise ValueError
        named = pwd.getpwnam(account.pw_name)
        if (named.pw_uid, named.pw_gid, named.pw_dir) != (uid, account.pw_gid, account.pw_dir):
            raise ValueError
        home = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in filter(None, account.pw_dir.split("/")):
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home)
                os.close(home)
                home = child
            info = os.fstat(home)
            if info.st_uid != uid or info.st_mode & 0o022:
                raise ValueError
        finally:
            os.close(home)
        return account
    except (KeyError, OSError, ValueError, TypeError, AttributeError):
        raise InstallRefused("invalid_normal_account") from None


def _sudo_account():
    if os.getuid() != 0 or os.geteuid() != 0:
        raise InstallRefused("root_staging_required")
    uid, gid = (os.environ.get(key, "") for key in ("SUDO_UID", "SUDO_GID"))
    if not re.fullmatch(r"[1-9][0-9]{0,9}", uid) or not re.fullmatch(r"[1-9][0-9]{0,9}", gid):
        raise InstallRefused("sudo_identity_refused")
    try:
        account = _normal_account(int(uid))
    except KeyError:
        raise InstallRefused("sudo_identity_refused") from None
    if account.pw_gid != int(gid) or account.pw_name != os.environ.get("SUDO_USER"):
        raise InstallRefused("sudo_identity_refused")
    return account


def _environment(account):
    return {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "HOME": "/root",
            "DEBIAN_FRONTEND": "noninteractive", "SUDO_UID": str(account.pw_uid),
            "SUDO_GID": str(account.pw_gid), "SUDO_USER": account.pw_name}


def _run(args, environment, *, timeout=30, limit=1024 * 1024):
    """Capture bounded stdout/stderr; timeout preserves an explicit failure state."""
    process = subprocess.Popen(args, env=environment, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               close_fds=True, start_new_session=True)
    captured = {"stdout": bytearray(), "stderr": bytearray()}
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise InstallRefused("command_timeout")
                for key, _ in selector.select(left):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    captured[key.data].extend(chunk)
                    if sum(map(len, captured.values())) > limit:
                        raise InstallRefused("command_output_too_large")
        code = process.wait(timeout=max(0, deadline - time.monotonic()))
        return code, bytes(captured["stdout"]), bytes(captured["stderr"])
    except subprocess.TimeoutExpired:
        raise InstallRefused("command_timeout") from None
    finally:
        _stop_group(process)
        process.stdout.close()
        process.stderr.close()


def _stop_group(process):
    # Only the session/process group created by this Popen belongs to us. The
    # leader may already be reaped while a child still holds the output pipe.
    try:
        os.killpg(process.pid, signal.SIGTERM)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            process.poll()
            os.killpg(process.pid, 0)
            time.sleep(0.02)
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _checked(args, environment, **options):
    code, out, err = _run(args, environment, **options)
    if code or err:
        raise InstallRefused("command_failed_or_warned")
    return out


def _root_directory(path):
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(path).parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
            info = os.fstat(current)
            if info.st_uid != 0 or info.st_gid != 0 or info.st_mode & 0o022:
                raise InstallRefused("untrusted_system_directory")
        return current
    except BaseException:
        os.close(current)
        raise


def _read_system(path, limit=65536):
    path = Path(path)
    parent = _root_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise InstallRefused("untrusted_system_file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            if len(data) > limit:
                raise InstallRefused("system_file_too_large")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _host(environment):
    if platform.system() != "Linux" or platform.machine() != "aarch64":
        raise InstallRefused("unsupported_host")
    model = _read_system("/sys/firmware/devicetree/base/model", 256).rstrip(b"\0\n")
    if not model.startswith(b"Raspberry Pi 5 Model B"):
        raise InstallRefused("unsupported_host")
    values = {}
    for line in _read_system("/usr/lib/os-release", 8192).decode("ascii").splitlines():
        key, _, value = line.partition("=")
        if key in ("ID", "VERSION_ID", "VERSION_CODENAME"):
            values[key] = value.strip('"')
    if (values.get("ID") not in ("debian", "raspbian") or values.get("VERSION_ID") != "13"
            or values.get("VERSION_CODENAME") != "trixie"
            or _checked(["/usr/bin/dpkg", "--print-architecture"], environment).strip() != b"arm64"):
        raise InstallRefused("unsupported_host")


def _validate_inventory(rows):
    if not isinstance(rows, dict) or not 1 <= len(rows) <= 100000:
        raise InstallRefused("invalid_dpkg_inventory")
    for name, row in rows.items():
        if (not isinstance(name, str) or len(name) > 256
                or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+(?::[a-z0-9-]+)?", name)
                or not isinstance(row, dict) or set(row) != {"status", "version", "architecture"}
                or row["status"] not in ("ii ", "hi ", "rc ", "un ")
                or not isinstance(row["version"], str) or len(row["version"]) > 256
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*|", row["version"])
                or not isinstance(row["architecture"], str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}|", row["architecture"])
                or row["status"] != "un " and (not row["version"] or not row["architecture"])):
            raise InstallRefused("dpkg_requires_recovery")
    names = {name.split(":")[0] for name, row in rows.items() if row["status"] in ("ii ", "hi ")}
    if not {"labwc", "network-manager", "systemd"} <= names:
        raise InstallRefused("required_desktop_runtime_missing")


def _package_state(environment):
    if _checked(["/usr/bin/dpkg", "--audit"], environment):
        raise InstallRefused("dpkg_requires_recovery")
    raw = _checked(["/usr/bin/dpkg-query", "-W",
                    "-f=${binary:Package}\t${db:Status-Abbrev}\t${Version}\t${Architecture}\n"],
                   environment, limit=8 * 1024 * 1024)
    rows = {}
    for line in raw.decode("ascii").splitlines():
        pieces = line.split("\t")
        if len(pieces) != 4 or pieces[0] in rows:
            raise InstallRefused("invalid_dpkg_inventory")
        name, status, version, architecture = pieces
        rows[name] = {"status": status, "version": version, "architecture": architecture}
    _validate_inventory(rows)
    return rows


def _installed_versions(rows):
    return {name: row["version"] for name, row in rows.items() if row["status"] in ("ii ", "hi ")}


def _fresh_inventory(rows):
    if any(name.split(":")[0] == "panelbridge" for name in rows):
        raise InstallRefused("existing_panelbridge_refused")
    return _installed_versions(rows)


def _inventory(environment):
    return _fresh_inventory(_package_state(environment))


def _native_locks():
    for path in ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock"):
        parent = _root_directory(Path(path).parent)
        try:
            try:
                fd = os.open(Path(path).name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                continue
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
                    raise InstallRefused("untrusted_package_lock")
                try:
                    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.lockf(fd, fcntl.LOCK_UN)
                except BlockingIOError:
                    raise InstallRefused("package_manager_busy") from None
            finally:
                os.close(fd)
        finally:
            os.close(parent)


def _source_security(data, deb822):
    # Read security values only. URI/auth fields are neither interpreted nor
    # copied to receipts; auth.conf and keyring contents are never opened.
    text = data.decode("utf-8")
    flags = {"trusted", "allow-insecure", "allow-weak", "allow-downgrade-to-insecure"}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if deb822:
            key, separator, value = stripped.partition(":")
            items = [(key.lower(), value.strip())] if separator else []
        else:
            match = re.match(r"^deb(?:-src)?\s+\[([^\]]*)\]", stripped)
            items = [part.split("=", 1) for part in match[1].split() if "=" in part] if match else []
        if any(key.lower() in flags and value.lower() not in ("no", "false", "0") for key, value in items):
            raise InstallRefused("repository_authentication_override")


def _repositories(environment):
    config = _checked(["/usr/bin/apt-config", "shell", "ROOT", "Dir", "ETC", "Dir::Etc", "LIST", "Dir::Etc::sourcelist",
                       "PARTS", "Dir::Etc::sourceparts"], environment, limit=4096)
    settings = {}
    for line in config.decode("ascii").splitlines():
        match = re.fullmatch(r"(ROOT|ETC|LIST|PARTS)='([^']*)'", line)
        if match is None:
            raise InstallRefused("unsupported_apt_source_configuration")
        settings[match[1]] = match[2]
    if (set(settings) != {"ROOT", "ETC", "LIST", "PARTS"} or settings["ROOT"] != "/"
            or settings["ETC"] not in ("etc/apt", "/etc/apt", "/etc/apt/")
            or settings["LIST"] != "sources.list" or settings["PARTS"] != "sources.list.d"):
        raise InstallRefused("unsupported_apt_source_configuration")
    try:
        _source_security(_read_system("/etc/apt/sources.list"), False)
    except FileNotFoundError:
        pass
    directory = _root_directory("/etc/apt/sources.list.d")
    try:
        names = os.listdir(directory)
        if len(names) > 128:
            raise InstallRefused("too_many_source_files")
        for name in names:
            if name.endswith((".list", ".sources")):
                _source_security(_read_system(Path("/etc/apt/sources.list.d") / name), name.endswith(".sources"))
    finally:
        os.close(directory)


APT_BASE = ["/usr/bin/apt-get", "--no-remove", "--no-upgrade", "--no-install-recommends",
            "-o", "APT::Get::AllowUnauthenticated=false", "-o", "Acquire::AllowInsecureRepositories=false",
            "-o", "Acquire::AllowWeakRepositories=false", "-o", "Acquire::AllowDowngradeToInsecureRepositories=false",
            "-o", "DPkg::Lock::Timeout=0"]


def _collisions(receipt):
    targets = {"/" + item["path"] for item in receipt["payload_files"] if not item["path"].startswith("DEBIAN/")}
    targets |= {"/etc/panelbridge", "/usr/lib/panelbridge", "/usr/share/panelbridge",
                "/etc/systemd/system/panelbridge-helper.service", "/etc/systemd/system/panelbridge-rescue.service",
                "/etc/dbus-1/system.d/org.panelbridge.Helper1.conf"}
    for target in targets:
        if os.path.lexists(target):
            raise InstallRefused("preexisting_installation_path")
        try:
            parent = _root_directory(Path(target).parent)
        except FileNotFoundError:
            continue
        else:
            os.close(parent)
    state_fd = _root_directory("/var/lib/panelbridge")
    try:
        if set(os.listdir(state_fd)) - {"installer-staging", "installer.lock", "installer-current.json"}:
            raise InstallRefused("preexisting_installation_state")
    finally:
        os.close(state_fd)


def _write(stage, name, content):
    fd = os.open(stage / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


MAX_JOURNAL = 32 * 1024 * 1024
PHASES = ("prepared", "package_transaction_prepared", "package_transaction_started",
          "installed_unconfigured", "configure_started", "awaiting_desktop_restart")


def _canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _private_read(path, limit=MAX_JOURNAL):
    path = Path(path)
    parent = _root_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
                    or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                    or not 0 < before.st_size <= limit):
                raise InstallRefused("untrusted_installer_receipt")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(limit + 1)
            after = os.fstat(fd)
            if (len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                    != (after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
                raise InstallRefused("installer_receipt_changed")
            return data
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def _optional_private(path, limit=MAX_JOURNAL):
    try:
        return _private_read(path, limit)
    except FileNotFoundError:
        return None


def _sync(directory):
    fd = _root_directory(directory)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_identical(stage, name, content):
    try:
        _write(stage, name, content)
        _sync(stage)
    except FileExistsError:
        if _private_read(stage / name, max(len(content), 1)) != content:
            raise InstallRefused("staged_input_conflict") from None


def _binding(stage, decoded, receipt, account):
    fingerprint = _digest(json.dumps([account.pw_uid, account.pw_gid, account.pw_name, account.pw_dir],
                                     separators=(",", ":")).encode())
    return {"artifact_sha256": _private_read(stage / "artifact.sha256", 64).decode("ascii"),
            "deb_sha256": _digest(decoded["package.deb"]), "source_sha256": _digest(decoded["source.tar.gz"]),
            "package_receipt_sha256": _digest(decoded["package-receipt.json"]),
            "package_version": receipt["version"], "enrolling_uid": account.pw_uid,
            "account_fingerprint": fingerprint}


def _validate_binding(value):
    hashes = {"artifact_sha256", "deb_sha256", "source_sha256", "package_receipt_sha256", "account_fingerprint"}
    if (not isinstance(value, dict) or set(value) != hashes | {"package_version", "enrolling_uid"}
            or any(not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k]) for k in hashes)
            or type(value["enrolling_uid"]) is not int or not 1000 <= value["enrolling_uid"] < 2**31
            or not isinstance(value["package_version"], str)
            or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]{0,255}", value["package_version"])):
        raise InstallRefused("invalid_attempt_binding")


def _validate_result(result, binding):
    _validate_binding(binding)
    keys = {"schema_version", "revision", "previous_sha256", "binding", "phase", "state", "reason", "baseline",
            "new_packages", "ready_for_install", "dependencies_rolled_back", "live_output_restored", "resume_supported"}
    if (not isinstance(result, dict) or set(result) != keys or result["schema_version"] != 2
            or type(result["revision"]) is not int or not 1 <= result["revision"] <= 128
            or result["binding"] != binding or result["phase"] not in PHASES
            or result["state"] not in (result["phase"], "installation_incomplete")
            or result["ready_for_install"] is not True or result["dependencies_rolled_back"] is not False
            or result["live_output_restored"] is not None or result["resume_supported"] is not True
            or (result["reason"] is not None and (not isinstance(result["reason"], str)
                or not re.fullmatch(r"[a-z_]{1,80}", result["reason"])))
            or (result["revision"] == 1 and result["previous_sha256"] is not None)
            or (result["revision"] > 1 and (not isinstance(result["previous_sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", result["previous_sha256"])))):
        raise InstallRefused("invalid_attempt_journal")
    baseline, additions = result["baseline"], result["new_packages"]
    _validate_inventory(baseline)
    _fresh_inventory(baseline)
    if result["phase"] == "prepared":
        if additions is not None:
            raise InstallRefused("invalid_attempt_plan")
        return
    if not isinstance(additions, list) or not 1 <= len(additions) <= 512:
        raise InstallRefused("invalid_attempt_plan")
    names = set()
    for row in additions:
        if (not isinstance(row, dict) or set(row) != {"package", "version", "architecture"}
                or not isinstance(row["package"], str) or len(row["package"]) > 256
                or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", row["package"])
                or row["package"] in names or row["architecture"] not in ("all", "arm64")
                or not isinstance(row["version"], str)
                or not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]{0,255}", row["version"])):
            raise InstallRefused("invalid_attempt_plan")
        names.add(row["package"])
    if (names & {name.split(":")[0] for name in _installed_versions(baseline)}
            or {"package": "panelbridge", "version": binding["package_version"], "architecture": "arm64"} not in additions):
        raise InstallRefused("invalid_attempt_plan")


def _journal_pair(previous, current, previous_raw):
    if (current["revision"] != previous["revision"] + 1 or current["previous_sha256"] != _digest(previous_raw)
            or current["baseline"] != previous["baseline"]
            or (previous["new_packages"] is not None and current["new_packages"] != previous["new_packages"])
            or not 0 <= PHASES.index(current["phase"]) - PHASES.index(previous["phase"]) <= 1):
        raise InstallRefused("conflicting_attempt_journal")


def _load_result(stage, binding):
    raw = _optional_private(stage / "result.json")
    pending = _optional_private(stage / "result.pending")
    current = _json(raw) if raw is not None else None
    fd = _root_directory(stage)
    try:
        journal_names = {name for name in os.listdir(fd) if name.startswith("result")}
    finally:
        os.close(fd)
    if current is not None:
        _validate_result(current, binding)
        # Immutable earlier revisions retain the original evidence and detect
        # missing/corrupted links, rather than selecting by filename or mtime.
        later = current
        for revision in range(current["revision"] - 1, 0, -1):
            earlier_raw = _private_read(stage / f"result-{revision:06d}.json")
            earlier = _json(earlier_raw)
            _validate_result(earlier, binding)
            _journal_pair(earlier, later, earlier_raw)
            later = earlier
    allowed = {"result.json", "result.pending"}
    if current is not None:
        allowed |= {f"result-{number:06d}.json" for number in range(1, current["revision"] + 1)}
        archived_current = _optional_private(stage / f"result-{current['revision']:06d}.json")
        if archived_current is not None and archived_current != raw:
            raise InstallRefused("conflicting_attempt_journal")
    if journal_names - allowed:
        raise InstallRefused("orphan_or_conflicting_attempt")
    if pending is not None:
        proposed = _json(pending)
        _validate_result(proposed, binding)
        if current is None:
            if proposed["revision"] != 1:
                raise InstallRefused("conflicting_attempt_journal")
        else:
            _journal_pair(current, proposed, raw)
            _write_identical(stage, f"result-{current['revision']:06d}.json", raw)
        os.replace(stage / "result.pending", stage / "result.json")
        _sync(stage)
        current = proposed
    return current


def _save_result(stage, result):
    previous = _load_result(stage, result["binding"])
    previous_raw = _private_read(stage / "result.json") if previous is not None else None
    result.update(revision=previous["revision"] + 1 if previous else 1,
                  previous_sha256=_digest(previous_raw) if previous else None)
    _validate_result(result, result["binding"])
    if previous is not None:
        _journal_pair(previous, result, previous_raw)
        _write_identical(stage, f"result-{previous['revision']:06d}.json", previous_raw)
    data = _canonical(result)
    if len(data) > MAX_JOURNAL:
        raise InstallRefused("attempt_journal_too_large")
    _write(stage, "result.pending", data)
    _sync(stage)
    os.replace(stage / "result.pending", stage / "result.json")
    _sync(stage)


def _trusted_stage(stage, expected):
    if stage.parent != STAGING or not re.fullmatch(r"[0-9a-f]{32}", stage.name):
        raise InstallRefused("untrusted_staging_path")
    fd = _root_directory(stage)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != 0o700:
            raise InstallRefused("untrusted_staging_path")
    finally:
        os.close(fd)
    if (_private_read(stage / "artifact.sha256", 64).decode("ascii") != expected
            or _digest(_private_read(stage / "installer.run", MAX_ARTIFACT)) != expected):
        raise InstallRefused("staged_artifact_changed")


def _select_attempt(incoming, binding):
    """Called under installer.lock; only this fixed reference grants resume authority."""
    base = STAGING.parent
    raw = _optional_private(base / "installer-current.json", 4096)
    pending = _optional_private(base / "installer-current.pending", 4096)
    if raw is not None and pending is not None:
        raise InstallRefused("conflicting_attempt_reference")
    reference = _json(raw if raw is not None else pending) if raw is not None or pending is not None else None
    if reference is not None:
        if (not isinstance(reference, dict) or set(reference) != {"schema_version", "attempt", "binding"}
                or type(reference["schema_version"]) is not int or reference["schema_version"] != 1
                or reference["binding"] != binding
                or not isinstance(reference["attempt"], str) or not re.fullmatch(r"[0-9a-f]{32}", reference["attempt"])):
            raise InstallRefused("attempt_binding_conflict")
        selected = STAGING / reference["attempt"]
    else:
        selected = incoming
    _validate_binding(binding)
    _trusted_stage(selected, binding["artifact_sha256"])
    directory = _root_directory(STAGING)
    try:
        names = os.listdir(directory)
    finally:
        os.close(directory)
    if len(names) > 128:
        raise InstallRefused("too_many_installer_attempts")
    for name in names:
        if not re.fullmatch(r"[0-9a-f]{32}", name):
            raise InstallRefused("unrecognized_installer_staging")
        fd = _root_directory(STAGING / name)
        try:
            journals = [item for item in os.listdir(fd) if item.startswith("result")]
        finally:
            os.close(fd)
        if journals and (reference is None or name != selected.name):
            raise InstallRefused("orphan_or_conflicting_attempt")
    if reference is None:
        reference = {"schema_version": 1, "attempt": selected.name, "binding": binding}
        _write(base, "installer-current.pending", _canonical(reference))
        _sync(base)
    if raw is None:
        os.replace(base / "installer-current.pending", base / "installer-current.json")
        _sync(base)
    return selected, _load_result(selected, binding)


def _apt_module(stage, snapshot):
    _write_identical(stage, "apt_plan.py", snapshot["packaging_tools/apt_plan.py"])
    spec = importlib.util.spec_from_file_location("panelbridge_installer_apt_plan", stage / "apt_plan.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _verify_installed(receipt):
    for entry in receipt["payload_files"]:
        if entry["path"].startswith("DEBIAN/"):
            hook = entry["path"].removeprefix("DEBIAN/")
            if hook not in ("postinst", "prerm", "postrm"):
                continue
            path = Path("/var/lib/dpkg/info") / ("panelbridge." + hook)
        else:
            path = Path("/") / entry["path"]
        parent = _root_directory(path.parent)
        try:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                before = os.fstat(fd)
                if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_gid != 0
                        or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != entry["mode"]
                        or before.st_size != entry["size"]):
                    raise InstallRefused("installed_payload_mismatch")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    content = stream.read(entry["size"] + 1)
                after = os.fstat(fd)
                if (_digest(content) != entry["sha256"] or len(content) != entry["size"]
                        or (before.st_mtime_ns, before.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                    raise InstallRefused("installed_payload_mismatch")
            finally:
                os.close(fd)
        finally:
            os.close(parent)


def _package_relation(actual, result):
    baseline, additions = result["baseline"], result["new_packages"]
    if actual == baseline:
        return "unchanged"
    if additions is None:
        return "partial_or_foreign"
    added = {item["package"]: item for item in additions}
    unchanged = {name: row for name, row in baseline.items() if name.split(":")[0] not in added}
    observed_old = {name: row for name, row in actual.items() if name.split(":")[0] not in added}
    if unchanged != observed_old:
        return "partial_or_foreign"
    for name, item in added.items():
        matches = [(key, row) for key, row in actual.items() if key.split(":")[0] == name]
        if len(matches) != 1:
            return "partial_or_foreign"
        key, row = matches[0]
        if (key not in (name, name + ":" + item["architecture"])
                or row != {"status": "ii ", "version": item["version"], "architecture": item["architecture"]}):
            return "partial_or_foreign"
    return "complete"


def _maintenance_binding(result):
    raw = _optional_private(STAGING.parent / "maintenance.json", 16384)
    if raw is None:
        if result["phase"] == "awaiting_desktop_restart":
            raise InstallRefused("maintenance_ownership_conflict")
        return
    journal = _json(raw)
    keys = {"schema_version", "normal_uid", "account_fingerprint", "phase", "initial_helper", "operations",
            "user_setup", "user_restore"}
    operations = {"enrollment", "user_configure", "reload", "helper_enable", "helper_start", "user_restore",
                  "helper_stop", "helper_disable", "enrollment_restore"}
    if (result["phase"] not in ("configure_started", "awaiting_desktop_restart")
            or not isinstance(journal, dict) or set(journal) != keys
            or type(journal["schema_version"]) is not int or journal["schema_version"] != 1
            or type(journal["normal_uid"]) is not int or journal["normal_uid"] != result["binding"]["enrolling_uid"]
            or journal["account_fingerprint"] != result["binding"]["account_fingerprint"]
            or journal["phase"] not in ("prepared", "configuring", "awaiting_desktop_restart", "rollback_pending", "rolled_back")
            or journal["initial_helper"] != {"active": False, "enabled": False}
            or not isinstance(journal["operations"], dict) or set(journal["operations"]) != operations
            or any(value not in ("not_started", "pending", "done") for value in journal["operations"].values())):
        raise InstallRefused("maintenance_ownership_conflict")
    for field, states in (("user_setup", ("configured",)), ("user_restore", ("restored", "not_configured"))):
        entry = journal[field]
        if entry is not None and (not isinstance(entry, dict) or set(entry) != {"state", "scope", "live_output_restored"}
                or entry["state"] not in states or entry["scope"] != "configuration_bytes_only"
                or entry["live_output_restored"] is not None):
            raise InstallRefused("maintenance_ownership_conflict")
    # The fixed installed API revalidates enrollment, service ownership, saved
    # originals, pending operations and current user edits at the point of use.


def _execute(stage, decoded, receipt, snapshot, account, *, resume=None):
    """Bounded first-install engine; root_main supplies the locked owned journal."""
    env = _environment(account)
    binding = _binding(stage, decoded, receipt, account)
    if resume is not None:
        _validate_result(resume, binding)
    result = json.loads(json.dumps(resume)) if resume is not None else None
    try:
        current_account = _normal_account(account.pw_uid)
        if _binding(stage, decoded, receipt, current_account) != binding:
            raise InstallRefused("attempt_binding_conflict")
        _host(env)
        _native_locks()
        actual = _package_state(env)
        if result is None:
            _fresh_inventory(actual)
            _collisions(receipt)
            result = {"schema_version": 2, "revision": 1, "previous_sha256": None, "binding": binding,
                      "phase": "prepared", "state": "prepared", "reason": None, "baseline": actual,
                      "new_packages": None, "ready_for_install": True, "dependencies_rolled_back": False,
                      "live_output_restored": None, "resume_supported": True}
            _save_result(stage, result)
        relation = _package_relation(actual, result)
        phase = PHASES.index(result["phase"])
        if phase < PHASES.index("package_transaction_started"):
            if relation != "unchanged":
                raise InstallRefused("package_state_partial_or_foreign")
            _collisions(receipt)
            _repositories(env)
            for name, data in decoded.items():
                _write_identical(stage, name, data)
            parser = _apt_module(stage, snapshot)
            deb = str(stage / "package.deb")
            def simulate(arguments, rows):
                output = _checked([*APT_BASE, "--simulate", "install", deb, *arguments], env, limit=262144)
                return parser.inspect_install_plan(output.decode("ascii"), expected_version=receipt["version"],
                                                   installed_versions=_installed_versions(rows))
            plan = simulate([], actual)
            additions = [{"package": item.package, "version": item.version, "architecture": item.architecture}
                         for item in plan.additions]
            if result["new_packages"] is not None and result["new_packages"] != additions:
                raise InstallRefused("apt_plan_changed")
            pins = [f"{item.package}{':arm64' if item.architecture == 'arm64' else ''}={item.version}"
                    for item in plan.additions if item.package != "panelbridge"]
            result.update(new_packages=additions, phase="package_transaction_prepared",
                          state="package_transaction_prepared", reason=None)
            _save_result(stage, result)
            _native_locks()
            fresh = _package_state(env)
            if fresh != actual or simulate(pins, fresh) != plan:
                raise InstallRefused("apt_plan_changed")
            result.update(phase="package_transaction_started", state="package_transaction_started")
            _save_result(stage, result)
            code, _, _ = _run([*APT_BASE, "--assume-yes", "install", deb, *pins], env, timeout=900, limit=8 * 1024 * 1024)
            if code:
                raise InstallRefused("package_transaction_failed")
            actual = _package_state(env)
            relation = _package_relation(actual, result)
        if relation != "complete":
            # Even unchanged dpkg rows after the APT-start marker cannot prove
            # that maintainer scripts or external effects never ran.
            raise InstallRefused("interrupted_package_transaction_requires_review" if relation == "unchanged"
                                 else "package_state_partial_or_foreign")
        _verify_installed(receipt)
        _maintenance_binding(result)
        if PHASES.index(result["phase"]) < PHASES.index("installed_unconfigured"):
            result.update(phase="installed_unconfigured", state="installed_unconfigured", reason=None)
            _save_result(stage, result)
        result.update(phase="configure_started" if result["phase"] != "awaiting_desktop_restart"
                      else "awaiting_desktop_restart", state="configure_started", reason=None)
        # Preserve monotonic phase for a repeated completed attempt.
        result["state"] = result["phase"]
        _save_result(stage, result)
        code, out, err = _run(["/usr/bin/python3", "-I", "-B", "/usr/lib/panelbridge/maintain-launch.py", "configure"],
                             env, timeout=120, limit=65536)
        if code or err or _json(out) != SUCCESS:
            raise InstallRefused("maintenance_configure_failed")
        result.update(phase="awaiting_desktop_restart", state="awaiting_desktop_restart", reason=None)
        _save_result(stage, result)
        return 0, result
    except (InstallRefused, OSError, ValueError, UnicodeError) as error:
        reason = str(error) if isinstance(error, InstallRefused) else "operation_refused"
        if result is None:
            return 4, {"state": "installation_incomplete", "phase": "preflight", "reason": reason,
                       "ready_for_install": False, "dependencies_rolled_back": False, "live_output_restored": None}
        result.update(state="installation_incomplete", reason=reason)
        try:
            _save_result(stage, result)
        except (InstallRefused, OSError, ValueError):
            # Preserve all durable evidence. Never overwrite a conflicting or
            # damaged pending journal just to make a failure receipt prettier.
            result["reason"] = "attempt_journal_requires_review"
        return 4, result


def _root_main(decoded, receipt, snapshot):
    account = _sudo_account()
    artifact = Path(__file__)
    stage = artifact.parent
    if artifact.name != "installer.run" or stage.parent != STAGING or not re.fullmatch(r"[0-9a-f]{32}", stage.name):
        raise InstallRefused("untrusted_staging_path")
    binding = _binding(stage, decoded, receipt, account)
    _trusted_stage(stage, binding["artifact_sha256"])
    base = _root_directory(STAGING.parent)
    lock = None
    try:
        lock = os.open("installer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=base)
        info = os.fstat(lock)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise InstallRefused("untrusted_installer_lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallRefused("installer_busy") from None
        selected, resume = _select_attempt(stage, binding)
        return _execute(selected, decoded, receipt, snapshot, account, resume=resume)
    finally:
        if lock is not None:
            os.close(lock)
        os.close(base)


def main(argv=None):
    arguments = sys.argv[1:] if argv is None else argv
    try:
        if arguments not in ([], ["--root-staged"]):
            raise InstallRefused("invalid_request")
        decoded, receipt, snapshot = _bundle()
        present = _lifecycle_present(receipt, snapshot)
        if not EXPERIMENTAL_INSTALL_ENABLED or not present:
            print(json.dumps({"state": "developer_unfinished", "ready_for_install": False,
                              "lifecycle_inputs_present": present, "publisher_authenticated": False}))
            return 3
        if arguments == ["--root-staged"]:
            code, result = _root_main(decoded, receipt, snapshot)
            print(json.dumps({key: result[key] for key in ("state", "phase", "reason", "ready_for_install",
                              "dependencies_rolled_back", "live_output_restored") if key in result}, sort_keys=True))
            return code
        if os.getuid() == 0 or os.geteuid() != os.getuid():
            raise InstallRefused("normal_user_required")
        _normal_account(os.getuid())
        artifact = Path(__file__).absolute()
        fd = os.open(artifact, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or info.st_size > MAX_ARTIFACT:
                raise InstallRefused("invalid_artifact_file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                data = stream.read(MAX_ARTIFACT + 1)
            if len(data) != info.st_size:
                raise InstallRefused("artifact_changed")
        finally:
            os.close(fd)
        return subprocess.call(["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B", "-c", ROOT_LOADER,
                                str(artifact), _digest(data)], env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (InstallRefused, OSError, UnicodeError) as error:
        reason = str(error) if isinstance(error, InstallRefused) else "operation_refused"
        print(json.dumps({"state": "installer_refused", "reason": reason, "ready_for_install": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
