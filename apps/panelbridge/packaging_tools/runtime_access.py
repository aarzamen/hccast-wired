"""Narrow desktop IPC trust: private runtime or the existing distro VNC service.

No socket is opened here. VNC sharing retains the existing user's desktop trust
boundary; it cannot provide the isolation of a private runtime directory.
"""
import errno
import os
import pwd
import re
import selectors
import stat
import struct
import subprocess
import time


class RuntimeAccessError(RuntimeError):
    pass


def _acl(fd, name):
    if not hasattr(os, "getxattr"):
        raise RuntimeAccessError("runtime_acl_unavailable")
    try:
        raw = os.getxattr(fd, name)
    except OSError as error:
        if error.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA), errno.ENOTSUP):
            return None
        raise RuntimeAccessError("runtime_acl_unavailable") from None
    if len(raw) < 4 or len(raw) > 4096 or (len(raw) - 4) % 8 or struct.unpack_from("<I", raw)[0] != 2:
        raise RuntimeAccessError("malformed_runtime_acl")
    entries = {}
    for offset in range(4, len(raw), 8):
        tag, perms, uid = struct.unpack_from("<HHI", raw, offset)
        if tag not in (1, 2, 4, 8, 16, 32) or perms > 7 or (tag, uid) in entries:
            raise RuntimeAccessError("malformed_runtime_acl")
        if (tag in (2, 8)) == (uid == 0xffffffff):
            raise RuntimeAccessError("malformed_runtime_acl")
        entries[tag, uid] = perms
    return entries


def _trusted_file(path):
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parts = path.split("/")[1:]
        for index, part in enumerate(parts):
            child = os.open(part, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if index < len(parts)-1 else 0), dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeAccessError("untrusted_vnc_service")
        if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
            raise RuntimeAccessError("untrusted_vnc_service")
    finally:
        os.close(fd)


def _service_show():
    fields = ("LoadState", "ActiveState", "SubState", "UnitFileState", "FragmentPath", "DropInPaths", "User", "Group", "ExecStart")
    args = ["/usr/bin/systemctl", "show", "--no-pager", *("--property=" + key for key in fields), "wayvnc.service"]
    process = None
    try:
        process = subprocess.Popen(args, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        data = bytearray()
        deadline = time.monotonic() + 3
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise RuntimeAccessError("vnc_service_timeout")
                for key, _ in selector.select(left):
                    chunk = os.read(key.fd, 4096)
                    if not chunk:
                        selector.unregister(key.fd)
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise RuntimeAccessError("vnc_service_reply_too_large")
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise RuntimeAccessError("vnc_service_unavailable")
        result = {}
        for line in data.decode("utf-8").splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in fields or key in result:
                raise RuntimeAccessError("invalid_vnc_service_reply")
            result[key] = value
        if set(result) != set(fields):
            raise RuntimeAccessError("invalid_vnc_service_reply")
        return result
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise RuntimeAccessError("vnc_service_unavailable") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            process.stdout.close()


def _vnc_uid():
    account = pwd.getpwnam("vnc")
    if (account.pw_name != "vnc" or not 0 < account.pw_uid < 1000 or not 0 < account.pw_gid < 1000
            or account.pw_shell not in ("/usr/sbin/nologin", "/sbin/nologin")
            or pwd.getpwuid(account.pw_uid).pw_name != "vnc"):
        raise RuntimeAccessError("unexpected_vnc_account")
    for path in ("/etc/passwd", "/usr/lib/systemd/system/wayvnc.service", "/usr/sbin/wayvnc-run.sh"):
        _trusted_file(path)
    values = _service_show()
    expected = {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled",
                "FragmentPath": "/usr/lib/systemd/system/wayvnc.service", "DropInPaths": "", "User": "vnc"}
    if any(values[key] != value for key, value in expected.items()) or values["Group"] not in ("", "vnc"):
        raise RuntimeAccessError("unexpected_vnc_service")
    # systemctl includes status/timestamps after the fixed argv; there must be
    # exactly one command, and no extra argument or shell expression.
    if not re.fullmatch(r"\{ path=/bin/sh ; argv\[\]=/bin/sh /usr/sbin/wayvnc-run\.sh ; ignore_errors=no ; [^{}\n]* \}", values["ExecStart"]):
        raise RuntimeAccessError("unexpected_vnc_command")
    return account.pw_uid


def validate_runtime(fd, owner):
    """Validate an already opened no-follow directory; re-run at each IPC boundary."""
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != owner:
            raise RuntimeAccessError("unsafe_user_runtime")
        if _acl(fd, "system.posix_acl_default") is not None:
            raise RuntimeAccessError("default_runtime_acl")
        access = _acl(fd, "system.posix_acl_access")
        base = {(1, 0xffffffff): 7, (4, 0xffffffff): 0, (32, 0xffffffff): 0}
        mode = stat.S_IMODE(info.st_mode)
        if mode == 0o700 and (access is None or access == base):
            return "private"
        if mode != 0o770 or access is None:
            raise RuntimeAccessError("unsafe_user_runtime")
        uid = _vnc_uid()
        if uid == owner or access != {**base, (2, uid): 7, (16, 0xffffffff): 7}:
            raise RuntimeAccessError("untrusted_runtime_acl")
        return "existing_vnc"
    except (OSError, KeyError, ValueError, TypeError):
        raise RuntimeAccessError("runtime_authority_unavailable") from None
