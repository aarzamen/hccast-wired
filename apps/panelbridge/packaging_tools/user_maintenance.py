"""Fixed normal-user maintenance commands; no activation or session changes.

The installed launcher uses isolated Python. No command-line input can select a
home, config path, command, user or fixture. The only subprocess reads compositor
output metadata; UserSetup owns the existing durable configuration transaction.
"""

import json
import math
import os
from pathlib import Path
import pwd
import re
import selectors
import stat
import subprocess
import sys
import time

from .user_setup import UserSetup
from .runtime_access import RuntimeAccessError, validate_runtime

CAPTURE_TIMEOUT = 3.0
MAX_CAPTURE_BYTES = 262144
_ACTIONS = ("configure", "restore", "status")
_TRANSFORMS = {"normal", "90", "180", "270", "flipped", "flipped-90", "flipped-180", "flipped-270"}


class DesktopUnavailable(Exception):
    """A fixed public reason, never raw command output or private paths."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _account():
    uid = os.getuid()
    if uid == 0 or os.geteuid() != uid:
        raise ValueError("normal_user_required")
    try:
        account = pwd.getpwuid(uid)
        raw = account.pw_dir
        if (account.pw_uid != uid or not isinstance(raw, str) or not raw.startswith("/")
                or raw == "/" or "\x00" in raw or any(part in (".", "..") for part in raw.split("/"))):
            raise ValueError
        home = Path(raw)
        info = home.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError
    except (KeyError, OSError, TypeError, ValueError):
        raise ValueError("account_home_unavailable") from None
    return uid, home


def _runtime_directory(uid):
    return Path("/run/user") / str(uid)


def _desktop_environment(uid):
    runtime = _runtime_directory(uid)
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if (os.environ.get("XDG_RUNTIME_DIR") != str(runtime)
            or os.environ.get("XDG_SESSION_TYPE", "wayland") != "wayland"
            or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}", display)):
        raise DesktopUnavailable("desktop_environment_unavailable")
    fd = None
    try:
        fd = os.open(runtime, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        validate_runtime(fd, uid)
        endpoint = os.stat(display, dir_fd=fd, follow_symlinks=False)
        if endpoint.st_uid != uid or not stat.S_ISSOCK(endpoint.st_mode):
            raise DesktopUnavailable("desktop_environment_unavailable")
    except (OSError, RuntimeAccessError):
        raise DesktopUnavailable("desktop_environment_unavailable") from None
    finally:
        if fd is not None:
            os.close(fd)
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "XDG_SESSION_TYPE": "wayland",
            "XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": display}


def _capture(environment):
    """Read at most 256 KiB from the fixed command, killing/reaping on failure."""
    process = None
    deadline = time.monotonic() + CAPTURE_TIMEOUT
    try:
        process = subprocess.Popen(
            ["/usr/bin/wlr-randr", "--json"], env=environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        data = bytearray()
        with selectors.DefaultSelector() as selector:
            os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DesktopUnavailable("capture_timeout")
                for key, _ in selector.select(remaining):
                    try:
                        chunk = os.read(key.fd, min(65536, MAX_CAPTURE_BYTES + 1 - len(data)))
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        data.extend(chunk)
                        if len(data) > MAX_CAPTURE_BYTES:
                            raise DesktopUnavailable("capture_too_large")
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            raise DesktopUnavailable("capture_timeout") from None
        if code != 0:
            raise DesktopUnavailable("capture_failed")
        return bytes(data)
    except OSError:
        raise DesktopUnavailable("capture_unavailable") from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    # Do not block forever on a kernel-stuck child. No mutation
                    # has occurred, and the public operation remains a failure.
                    pass
            if process.stdout is not None:
                process.stdout.close()


def _number(value, minimum, maximum):
    return type(value) in (int, float) and math.isfinite(value) and minimum < value <= maximum


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Nonfinite JSON value")


def _outputs(raw):
    """Validate every enabled baseline before UserSetup can create its journal."""
    try:
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_CAPTURE_BYTES:
            raise ValueError
        outputs = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant,
                             object_pairs_hook=_json_object)
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 32:
            raise ValueError
        selected, names = [], set()
        for output in outputs:
            if (not isinstance(output, dict) or not isinstance(output.get("name"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", output["name"])
                    or output["name"] in names or type(output.get("enabled")) is not bool):
                raise ValueError
            names.add(output["name"])
            if not output["enabled"]:
                continue
            modes = output.get("modes")
            if (not isinstance(modes, list) or not 1 <= len(modes) <= 256
                    or any(not isinstance(mode, dict) or type(mode.get("current", False)) is not bool for mode in modes)):
                raise ValueError
            current = [mode for mode in modes if mode.get("current")]
            if len(current) != 1:
                raise ValueError
            mode = current[0]
            if (any(type(mode.get(key)) is not int or not 1 <= mode[key] <= 16384 for key in ("width", "height"))
                    or not _number(mode.get("refresh"), 0, 1000)
                    or not _number(output.get("scale"), 0, 4)
                    or not isinstance(output.get("transform"), str)
                    or output["transform"] not in _TRANSFORMS):
                raise ValueError
            position = output.get("position")
            if (not isinstance(position, dict)
                    or any(type(position.get(key)) is not int or abs(position[key]) > 65536 for key in ("x", "y"))):
                raise ValueError
            selected.append({"name": output["name"], "enabled": True,
                             "modes": [{key: mode[key] for key in ("width", "height", "refresh", "current")}],
                             "position": {key: position[key] for key in ("x", "y")},
                             "scale": output["scale"], "transform": output["transform"]})
        if not selected:
            raise ValueError
        UserSetup.layout(selected)  # Validate downstream formatting before writes.
        return selected
    except (UnicodeError, ValueError, TypeError, KeyError, OverflowError, RecursionError, RuntimeError):
        raise DesktopUnavailable("invalid_desktop_output") from None


def _receipt(action, state, **extra):
    return {"api_version": 1, "action": action, "state": state,
            "scope": "configuration_bytes_only", "live_output_restored": None, **extra}


def _state_receipt(action, status):
    conflicts = len(status["conflicts"])
    state = ("conflict" if conflicts else "configured" if status["config_prepared"] else
             "restored" if status["state"] == "uninstalled" else
             "not_configured" if status["state"] == "not_installed" else "incomplete")
    return _receipt(action, state, journal_state=status["state"],
                    config_prepared=status["config_prepared"], conflict_count=conflicts)


def _perform(action):
    setup = None
    try:
        uid, home = _account()
    except ValueError as error:
        return 5, _receipt(action, "identity_refused", reason=str(error))
    try:
        if action == "configure":
            outputs = _outputs(_capture(_desktop_environment(uid)))
        setup = UserSetup(home / ".config")
        if action == "configure":
            status = setup.install(outputs)
        elif action == "restore":
            # UserSetup.uninstall creates a lock. Skip it when there is no
            # journal, keeping an absent installation entirely read-only.
            status = setup.status()
            if status["state"] != "not_installed":
                status = setup.uninstall()
        else:
            status = setup.status()
        receipt = _state_receipt(action, status)
        return (3 if receipt["state"] in ("conflict", "incomplete") else 0), receipt
    except DesktopUnavailable as error:
        return 4, _receipt(action, "desktop_unavailable", reason=error.reason)
    except RuntimeError:
        # Existing transactions deliberately use RuntimeError for conflicts,
        # unsafe paths and invalid journals. Never serialize their raw message.
        count = None
        try:
            if setup is not None:
                count = len(setup.status()["conflicts"]) or None
        except (RuntimeError, OSError):
            pass
        return 3, _receipt(action, "conflict", reason="configuration_requires_reconciliation", conflict_count=count)
    except OSError:
        return 6, _receipt(action, "operation_failed", reason="configuration_io_failed")


def main(argv=None):
    """Emit one privacy-safe JSON receipt. Exit codes are documented with the seam."""
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1 or arguments[0] not in _ACTIONS:
        code, receipt = 2, _receipt(None, "invalid_request", reason="expected_configure_restore_or_status")
    else:
        code, receipt = _perform(arguments[0])
    print(json.dumps(receipt, sort_keys=True, allow_nan=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
