"""Fixed-path normal-user configuration transactions; no compositor operations."""

import base64
import binascii
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

BEGIN = b"\n# BEGIN PanelBridge managed output\n"
END = b"# END PanelBridge managed output\n"
ENV = BEGIN + b"WLR_BACKENDS=drm,libinput,headless\nWLR_HEADLESS_OUTPUTS=1\n" + END
TARGETS = ("labwc/environment", "kanshi/config")
AUTOSTART_PATH = "autostart/panelbridge-session.desktop"
# Frozen previous content is retained for exact-byte upgrade and restoration.
LEGACY_AUTOSTART = b"""[Desktop Entry]
Type=Application
Name=PanelBridge desktop session
Exec=/usr/lib/panelbridge/session-launch
Terminal=false
NoDisplay=true
OnlyShowIn=labwc;LXDE;
X-GNOME-Autostart-enabled=true
"""
# Frozen installed content from packaging/panelbridge-session.desktop.
AUTOSTART = b"""[Desktop Entry]
Type=Application
Name=PanelBridge desktop session
Exec=/usr/lib/panelbridge/session-launch
Terminal=false
NoDisplay=true
OnlyShowIn=labwc;LXDE;rpd-wayland;
X-GNOME-Autostart-enabled=true
"""
JOURNAL = "panelbridge/desktop-install.json"
LOCK = "panelbridge/desktop-install.lock"
ALL_TARGETS = (*TARGETS, AUTOSTART_PATH)
DIRECTORIES = {"labwc", "kanshi", "autostart"}
MAX_CONTENT = 524288
MAX_JOURNAL = 8 * 1024 * 1024


def _user():
    if os.getuid() == 0 or os.geteuid() == 0 or os.getuid() != os.geteuid():
        raise RuntimeError("Configuration setup requires an unprivileged user, never root")


def _encoded(data):
    return None if data is None else base64.b64encode(data).decode("ascii")


def _decoded(data):
    if data is None:
        return None
    if not isinstance(data, str) or len(data) > 4 * MAX_CONTENT // 3 + 4:
        raise ValueError("Invalid encoded configuration")
    value = base64.b64decode(data, validate=True)
    if len(value) > MAX_CONTENT:
        raise ValueError("Configuration is too large")
    return value


def _content(snapshot):
    return None if snapshot is None else snapshot["data"]


def _restored(data, item, *, allow_predecessor=False):
    before, applied, block = (_decoded(item[key]) for key in ("before", "applied", "block"))
    if data == before or (block is None and (data == applied or (
            allow_predecessor and applied == AUTOSTART and data == LEGACY_AUTOSTART))):
        return before
    if (data is not None and block is not None
            and data.count(block) == data.count(BEGIN) == data.count(END) == 1):
        target = data.replace(block, b"", 1)
        return None if before is None and not target else target
    raise ValueError("Managed content changed")


class _Files:
    def __init__(self, base, create):
        self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        self.private_fd = None
        try:
            for index, part in enumerate(base.parts[1:]):
                if create and index == len(base.parts) - 2:
                    try:
                        os.stat(part, dir_fd=self.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        self._owned_directory(self.fd, private_outer=self._private_ancestor())
                        try:
                            os.mkdir(part, 0o700, dir_fd=self.fd)
                            os.fsync(self.fd)
                        except FileExistsError:
                            pass
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=self.fd)
                os.close(self.fd)
                self.fd = next_fd
                info = os.fstat(self.fd)
                if info.st_uid == os.getuid() and not info.st_mode & 0o033:
                    if self.private_fd is not None:
                        os.close(self.private_fd)
                    self.private_fd = os.dup(self.fd)
            self.private_outer = self._private_ancestor()
            self._owned_directory(self.fd, private_outer=self.private_outer)
        except BaseException:
            os.close(self.fd)
            if self.private_fd is not None:
                os.close(self.private_fd)
            raise

    def _private_ancestor(self):
        if self.private_fd is None:
            return False
        info = os.fstat(self.private_fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o033:
            raise RuntimeError("Private configuration ancestor is no longer protected")
        return True

    def protected_outer(self):
        private = self._private_ancestor()
        self._owned_directory(self.fd, private_outer=private)
        return private

    def protected_parent(self, fd):
        self._owned_directory(fd, private_outer=self.protected_outer())

    @staticmethod
    def _owned_directory(fd, *, private_outer=False):
        info = os.fstat(fd)
        forbidden_write = 0o002 if private_outer else 0o022
        if info.st_uid != os.getuid() or info.st_mode & forbidden_write:
            raise RuntimeError("Configuration directories must be owned and protected from other writers")
        return info

    def parent(self, relative, create=False):
        if relative not in (*ALL_TARGETS, JOURNAL, LOCK):
            raise RuntimeError("Only fixed managed configuration paths are permitted")
        self.protected_outer()
        name = relative.split("/")[0]
        if create:
            self.protected_outer()
            try:
                os.mkdir(name, 0o700, dir_fd=self.fd)
                os.fsync(self.fd)
            except FileExistsError:
                pass
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            # A held, owned directory with no non-owner traversal shields
            # existing umask-0002 descendants. Recheck it on each access.
            self.protected_parent(fd)
            return fd
        except BaseException:
            os.close(fd)
            raise

    def read(self, relative):
        try:
            parent = self.parent(relative)
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(relative.split("/")[1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                self.protected_parent(parent)
                info = os.fstat(fd)
                limit = MAX_JOURNAL if relative == JOURNAL else MAX_CONTENT
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.getuid() or info.st_size > limit
                        or info.st_mode & 0o7000
                        or (relative in (JOURNAL, LOCK) and info.st_mode & 0o022)):
                    raise RuntimeError("Configuration authority must be a bounded owned regular file")
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    data = stream.read(limit + 1)
                after = os.fstat(fd)
                if (len(data) > limit or len(data) != info.st_size
                        or (info.st_mtime_ns, info.st_ctime_ns) != (after.st_mtime_ns, after.st_ctime_ns)):
                    raise RuntimeError("Configuration changed while reading")
                self.protected_parent(parent)
                return {"data": data, "mode": stat.S_IMODE(info.st_mode), "gid": info.st_gid,
                        "device": info.st_dev, "inode": info.st_ino}
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    def replace(self, relative, data, mode, gid, expected):
        if self.read(relative) != expected:
            raise RuntimeError("Configuration changed before write; preserving user changes")
        parent = self.parent(relative)
        name = relative.split("/")[1]
        temporary = ".panelbridge-" + uuid.uuid4().hex + ".tmp"
        created = None
        try:
            if data is None:
                self.protected_parent(parent)
                os.unlink(name, dir_fd=parent)
            else:
                self.protected_parent(parent)
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=parent)
                try:
                    created = os.fstat(fd)
                    if gid is not None:
                        os.fchown(fd, -1, gid)
                    os.fchmod(fd, mode)
                    with os.fdopen(fd, "wb", closefd=False) as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(fd)
                finally:
                    os.close(fd)
                if self.read(relative) != expected:
                    raise RuntimeError("Configuration changed before replace; preserving user changes")
                self.protected_parent(parent)
                os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
                created = None
            os.fsync(parent)
        finally:
            if created is not None:
                try:
                    self.protected_parent(parent)
                    current = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
            os.close(parent)


class DesktopSetup:
    include_autostart = False

    def __init__(self, config_dir):
        _user()
        self.base = Path(config_dir).absolute()
        if ".." in self.base.parts:
            raise RuntimeError("Configuration path traversal is forbidden")
        self.journal = self.base / JOURNAL

    @contextmanager
    def _transaction(self, create):
        _user()
        files = lock = None
        try:
            try:
                files = _Files(self.base, create)
            except FileNotFoundError:
                if create:
                    raise
                yield None
                return
            if create:
                parent = files.parent(LOCK, create=True)
                try:
                    files.protected_parent(parent)
                    lock = os.open("desktop-install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                                   | os.O_NONBLOCK, 0o600, dir_fd=parent)
                finally:
                    os.close(parent)
                authority = files.read(LOCK)
                info = os.fstat(lock)
                if authority is None or (info.st_dev, info.st_ino) != (
                        authority["device"], authority["inode"]):
                    raise RuntimeError("Transaction lock changed before locking")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise RuntimeError("Another configuration transaction is running") from None
            self._files = files
            yield files
        except OSError as error:
            if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.ENXIO):
                raise RuntimeError("Refusing unsafe or symlink configuration path") from None
            raise
        finally:
            if lock is not None:
                os.close(lock)
            if files is not None:
                os.close(files.fd)
                if files.private_fd is not None:
                    os.close(files.private_fd)

    def _save(self, record):
        raw = (json.dumps(record, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()
        if len(raw) > MAX_JOURNAL:
            raise RuntimeError("Installation journal exceeds its size bound")
        self._files.replace(JOURNAL, raw, 0o600, None, self._journal_snapshot)
        self._journal_snapshot = self._files.read(JOURNAL)

    def _record(self):
        self._journal_snapshot = self._files.read(JOURNAL)
        if self._journal_snapshot is None:
            return None
        try:
            record = json.loads(self._journal_snapshot["data"])
            if (not isinstance(record, dict) or type(record.get("api_version")) is not int
                    or record["api_version"] not in (1, 2)
                    or record.get("state") not in ("prepared", "installed", "restoring", "uninstalled")
                    or not isinstance(record.get("files"), dict)
                    or set(record["files"]) not in (set(TARGETS), set(ALL_TARGETS))):
                raise ValueError
            old = record["api_version"] == 1
            allowed = {"api_version", "state", "files"} if old else {
                "api_version", "state", "files", "created_dirs", "restore"}
            if (set(record) != allowed or (old and (record["state"] == "restoring"
                                                  or set(record["files"]) != set(TARGETS)))):
                raise ValueError
            for relative, item in record["files"].items():
                keys = {"before", "applied", "block", "mode", "sha256"}
                if not isinstance(item, dict) or set(item) != (keys if old else keys | {"gid"}):
                    raise ValueError
                before, applied, block = (_decoded(item[key]) for key in ("before", "applied", "block"))
                if (type(item["mode"]) is not int or not 0 <= item["mode"] <= 0o777
                        or applied is None or hashlib.sha256(applied).hexdigest() != item["sha256"]):
                    raise ValueError
                if relative == AUTOSTART_PATH:
                    if (block is not None or applied not in (LEGACY_AUTOSTART, AUTOSTART)
                            or before not in (None, LEGACY_AUTOSTART, applied)):
                        raise ValueError
                elif (block is None or not block.startswith(BEGIN) or not block.endswith(END)
                      or block.count(BEGIN) != 1 or block.count(END) != 1
                      or applied != (before or b"") + block
                      or (relative == TARGETS[0] and block != ENV)):
                    raise ValueError
                if old:
                    item["gid"] = None
                if item["gid"] is not None and (type(item["gid"]) is not int or item["gid"] < 0):
                    raise ValueError
            if old:
                record.update(api_version=2, created_dirs={}, restore={})
            if (not isinstance(record["created_dirs"], dict)
                    or not set(record["created_dirs"]) <= DIRECTORIES):
                raise ValueError
            for item in record["created_dirs"].values():
                if (not isinstance(item, dict) or set(item) != {"device", "inode"}
                        or any(value is not None and (type(value) is not int or value < 0)
                               for value in item.values())):
                    raise ValueError
            restore = record["restore"]
            if not isinstance(restore, dict) or (restore and set(restore) != set(record["files"])):
                raise ValueError
            if ((record["state"] == "restoring" and not restore)
                    or (record["state"] in ("prepared", "installed") and restore)):
                raise ValueError
            for relative, item in restore.items():
                if not isinstance(item, dict) or set(item) != {"from", "to", "mode", "gid"}:
                    raise ValueError
                original = record["files"][relative]
                # Restoration may have begun during a prepared template upgrade.
                if (_decoded(item["to"]) != _restored(_decoded(item["from"]), original,
                                                      allow_predecessor=True)
                        or type(item["mode"]) is not int or item["mode"] != original["mode"]
                        or type(item["gid"]) is not type(original["gid"])
                        or item["gid"] != original["gid"]):
                    raise ValueError
            return record
        except (ValueError, TypeError, KeyError, binascii.Error):
            raise RuntimeError("Invalid bounded installation journal; preserving configuration") from None

    def _item(self, current, block):
        before = _content(current)
        if block is None:
            if before not in (None, LEGACY_AUTOSTART, AUTOSTART):
                raise RuntimeError("Unrelated preexisting autostart; preserving file")
            applied = AUTOSTART
        else:
            if BEGIN in (before or b"") or END in (before or b""):
                raise RuntimeError("Unjournaled managed block; preserving configuration")
            applied = (before or b"") + block
        if len(applied) > MAX_CONTENT:
            raise RuntimeError("Configuration exceeds its size bound")
        return {"before": _encoded(before), "applied": _encoded(applied), "block": _encoded(block),
                "mode": current["mode"] if current else 0o600,
                "gid": current["gid"] if current else None,
                "sha256": hashlib.sha256(applied).hexdigest()}

    def _install_plan(self, record):
        plan, conflicts = {}, []
        for relative, item in record["files"].items():
            current = self._files.read(relative)
            data = _content(current)
            before, applied, block = (_decoded(item[key]) for key in ("before", "applied", "block"))
            if current and (current["mode"] != item["mode"]
                            or item["gid"] is not None and current["gid"] != item["gid"]):
                conflicts.append(relative)
            elif data == applied or (block is not None and data is not None
                                    and data.count(block) == data.count(BEGIN) == data.count(END) == 1):
                continue
            elif record["state"] == "prepared" and (data == before or (
                    relative == AUTOSTART_PATH and applied == AUTOSTART and data == LEGACY_AUTOSTART)):
                plan[relative] = (applied, item["mode"], item["gid"], current)
            else:
                conflicts.append(relative)
        return plan, conflicts

    def _restore_plan(self, record):
        plan, conflicts = {}, []
        for relative, item in record["files"].items():
            current = self._files.read(relative)
            data = _content(current)
            if record["state"] == "restoring":
                saved = record["restore"][relative]
                original, target = _decoded(saved["from"]), _decoded(saved["to"])
                mode, gid = saved["mode"], saved["gid"]
                if data not in (original, target):
                    conflicts.append(relative)
                    continue
            else:
                mode, gid = item["mode"], item["gid"]
                try:
                    target = _restored(data, item, allow_predecessor=record["state"] == "prepared")
                except ValueError:
                    conflicts.append(relative)
                    continue
            if current and (current["mode"] != mode or gid is not None and current["gid"] != gid):
                conflicts.append(relative)
                continue
            plan[relative] = (target, mode, gid, current)
        return plan, conflicts

    def _ensure_directory(self, relative, record):
        name = relative.split("/")[0]
        try:
            fd = self._files.parent(relative)
        except FileNotFoundError:
            record["created_dirs"][name] = {"device": None, "inode": None}
            self._save(record)  # Intent precedes mkdir; an unconfirmed directory is retained.
            try:
                self._files.protected_outer()
                os.mkdir(name, 0o700, dir_fd=self._files.fd)
            except FileExistsError:
                return  # A competing creator does not become app-owned.
            os.fsync(self._files.fd)
            fd = self._files.parent(relative)
            try:
                info = os.fstat(fd)
                record["created_dirs"][name] = {"device": info.st_dev, "inode": info.st_ino}
            finally:
                os.close(fd)
            self._save(record)
        else:
            os.close(fd)

    def _write_target(self, relative, data, mode, gid, expected):
        self._files.replace(relative, data, mode, gid, expected)

    def _cleanup_directories(self, record):
        for name, identity in record["created_dirs"].items():
            if identity["device"] is None or identity["inode"] is None:
                continue
            try:
                self._files.protected_outer()
                info = os.stat(name, dir_fd=self._files.fd, follow_symlinks=False)
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                        or (info.st_dev, info.st_ino) != (identity["device"], identity["inode"])):
                    continue
                self._files.protected_outer()
                os.rmdir(name, dir_fd=self._files.fd)  # Only empty, identity-matched directories.
                os.fsync(self._files.fd)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise

    def install(self, outputs):
        with self._transaction(create=True):
            record = self._record()
            if record and record["state"] == "restoring":
                raise RuntimeError("Resume uninstall restoration before installing again")
            if not record or record["state"] == "uninstalled":
                env = _content(self._files.read(TARGETS[0])) or b""
                if re.search(rb"^\s*(?:export\s+)?WLR_(?:BACKENDS|HEADLESS_OUTPUTS)\s*=", env, re.M):
                    raise RuntimeError("Existing backend customization needs reconciliation")
                blocks = dict(zip(TARGETS, (ENV, self.layout(outputs))))
                if self.include_autostart:
                    blocks[AUTOSTART_PATH] = None
                items = {relative: self._item(self._files.read(relative), block)
                         for relative, block in blocks.items()}
                record = {"api_version": 2, "state": "prepared", "files": items,
                          "created_dirs": {}, "restore": {}}
                self._save(record)
            elif self.include_autostart and AUTOSTART_PATH not in record["files"]:
                _, conflicts = self._install_plan(record)
                if conflicts:
                    raise RuntimeError("Desktop configuration changed; preserving user changes")
                record["files"][AUTOSTART_PATH] = self._item(self._files.read(AUTOSTART_PATH), None)
                record["state"] = "prepared"
                self._save(record)
            elif (self.include_autostart
                  and _decoded(record["files"][AUTOSTART_PATH]["applied"]) == LEGACY_AUTOSTART):
                _, conflicts = self._install_plan(record)
                if conflicts:
                    raise RuntimeError("Desktop configuration changed; preserving user changes")
                item = record["files"][AUTOSTART_PATH]
                item["applied"] = _encoded(AUTOSTART)
                item["sha256"] = hashlib.sha256(AUTOSTART).hexdigest()
                # Keep the first original and metadata. Until installed commits,
                # either exact template can be resumed or restored after a crash.
                record["state"] = "prepared"
                self._save(record)
            plan, conflicts = self._install_plan(record)
            if conflicts:
                raise RuntimeError("Configuration conflict; preserving user changes: " + ", ".join(conflicts))
            for relative, values in plan.items():
                self._ensure_directory(relative, record)
                self._write_target(relative, *values)
            if record["state"] != "installed":
                record["state"] = "installed"
                self._save(record)
            return self._status(record)

    def uninstall(self):
        with self._transaction(create=True):
            record = self._record()
            if not record:
                return self._status(None)
            if record["state"] != "uninstalled":
                plan, conflicts = self._restore_plan(record)
                if conflicts:
                    raise RuntimeError("Configuration changed; preserving conflicts: " + ", ".join(conflicts))
                if record["state"] != "restoring":
                    record["restore"] = {relative: {"from": _encoded(_content(values[3])),
                                                    "to": _encoded(values[0]), "mode": values[1],
                                                    "gid": values[2]}
                                         for relative, values in plan.items()}
                    record["state"] = "restoring"
                    self._save(record)  # Complete restoration targets precede every mutation.
                for relative, values in plan.items():
                    if _content(values[3]) != values[0]:
                        self._write_target(relative, *values)
                record["state"] = "uninstalled"
                self._save(record)
            self._cleanup_directories(record)
            return self._status(record)

    def _status(self, record):
        conflicts = []
        if record and record["state"] != "uninstalled":
            _, conflicts = (self._restore_plan(record) if record["state"] == "restoring"
                            else self._install_plan(record))
        return {"state": record["state"] if record else "not_installed", "conflicts": conflicts,
                "config_prepared": bool(record and record["state"] == "installed" and not conflicts),
                "live_output_restored": None}

    def status(self):
        with self._transaction(create=False) as files:
            return self._status(self._record()) if files else self._status(None)

    @staticmethod
    def layout(outputs):
        lines = []
        right = 0
        for output in outputs:
            if not output.get("enabled") or output["name"].startswith(("HEADLESS-", "NOOP-")):
                continue
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", output["name"]):
                raise RuntimeError("Unsupported output name")
            mode = next((m for m in output.get("modes", []) if m.get("current")), None)
            if not mode:
                raise RuntimeError("Enabled output has no current mode")
            width, height = int(mode["width"]), int(mode["height"])
            refresh = float(mode["refresh"])
            scale = float(output.get("scale", 1))
            transform = output.get("transform", "normal")
            if (
                transform
                not in (
                    "normal",
                    "90",
                    "180",
                    "270",
                    "flipped",
                    "flipped-90",
                    "flipped-180",
                    "flipped-270",
                )
                or not 0 < scale <= 4
                or not 0 < refresh <= 1000
            ):
                raise RuntimeError("Unsupported output baseline")
            x = int(output.get("position", {}).get("x", 0))
            y = int(output.get("position", {}).get("y", 0))
            logical_width = height if transform.endswith(("90", "270")) else width
            right = max(right, x + round(logical_width / scale))
            lines.append(
                f"  output {output['name']} enable mode {width}x{height}@{refresh:.6f}Hz position {x},{y} scale {scale:g} transform {transform}\n"
            )
        headless = "  output HEADLESS-1 enable mode --custom 1280x720@30Hz position {x},0 scale 2 transform normal\n"
        text = "profile panelbridge_desktop {\n" + "".join(lines) + headless.format(x=right) + "}\n"
        if lines:
            text += "profile panelbridge_headless {\n" + headless.format(x=0) + "}\n"
        return BEGIN + text.encode() + END
