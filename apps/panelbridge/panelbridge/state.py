"""Private user configuration with atomic writes and explicit mode trials."""

import hashlib
from functools import wraps
import json
import os
from pathlib import Path
import re
import stat
import tempfile

from .models import Profile


def durable_change(method):
    """Do not report an in-memory setting as saved if persistence failed."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        before_hash = self._disk_hash
        before = {key: getattr(self, key) for key in (
            "profile", "known_good", "selected_device", "failures", "pending",
            "recovered_trial", "config_error",
        )}
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            # Once replace succeeds the new bytes are visible, even if directory
            # fsync fails. Keep memory consistent and surface the durability error.
            if self._disk_hash == before_hash:
                for key, value in before.items():
                    setattr(self, key, value)
            raise
    return wrapped


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.profile = Profile()
        self.known_good = self.profile
        self.selected_device = None
        self.failures = 0
        self.pending = None
        self.recovered_trial = False
        self.config_error = None
        self._disk_hash = None
        if self.path.is_symlink():
            raise ValueError("Configuration cannot be a symlink")
        if not self.path.exists():
            return
        if not stat.S_ISREG(self.path.stat().st_mode):
            raise ValueError("Configuration must be a regular file")
        raw = self.path.read_bytes()
        self._disk_hash = hashlib.sha256(raw).hexdigest()
        try:
            if len(raw) > 65536:
                raise ValueError("Configuration exceeds size limit")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Configuration must be an object")
            if data.get("api_version") != 1:
                raise ValueError("Unsupported configuration version")
            profile = Profile.from_dict(data["profile"])
            known_good = Profile.from_dict(data["known_good"])
            device = self.validate_device(data.get("selected_device"))
            failures = data.get("failures", 0)
            if type(failures) is not int or not 0 <= failures <= 3:
                raise ValueError("Invalid failure count")
            self.profile = profile
            self.known_good = known_good
            self.selected_device = device
            self.failures = failures
            self.recovered_trial = data.get("pending") is not None
            if self.recovered_trial:
                self.save()
        except (ValueError, KeyError, TypeError) as error:
            self.config_error = str(error)
            self.profile = self.known_good = Profile()

    @staticmethod
    def validate_device(device):
        if device is None:
            return None
        if not isinstance(device, dict) or set(device) != {"address", "name"}:
            raise ValueError("Invalid selected receiver")
        if not isinstance(device["address"], str) or not re.fullmatch(
            r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", device["address"]
        ):
            raise ValueError("Invalid receiver address")
        if (
            not isinstance(device["name"], str)
            or not 1 <= len(device["name"]) <= 80
            or any(ord(c) < 32 for c in device["name"])
        ):
            raise ValueError("Invalid receiver name")
        return {"address": device["address"].lower(), "name": device["name"]}

    @property
    def recovery_required(self):
        return self.failures >= 3

    @property
    def effective_profile(self):
        return self.pending["profile"] if self.pending else self.profile

    def save(self):
        if self.config_error:
            raise RuntimeError("Preserving broken configuration until Restore Defaults")
        if self.path.is_symlink():
            raise ValueError("Configuration became a symlink")
        current = hashlib.sha256(self.path.read_bytes()).hexdigest() if self.path.exists() else None
        if current != self._disk_hash:
            raise RuntimeError("Configuration changed in another process")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = {
            "api_version": 1,
            "profile": self.profile.to_dict(),
            "known_good": self.known_good.to_dict(),
            "selected_device": self.selected_device,
            "failures": self.failures,
            "pending": None
            if not self.pending
            else {
                "profile": self.pending["profile"].to_dict(),
                "deadline": self.pending["deadline"],
            },
        }
        raw = (json.dumps(data, indent=2, allow_nan=False) + "\n").encode()
        fd, tmp = tempfile.mkstemp(prefix=".panelbridge-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as file:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp, self.path)
            self._disk_hash = hashlib.sha256(raw).hexdigest()
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @durable_change
    def select_device(self, address, name):
        self.selected_device = self.validate_device({"address": address, "name": name})
        self.save()

    @durable_change
    def begin_trial(self, profile, now):
        if self.pending:
            raise RuntimeError("Confirm or revert the existing display trial first")
        if not isinstance(profile, Profile):
            raise ValueError("Validated profile required")
        self.pending = {"profile": profile, "deadline": now + 20}
        self.save()

    @durable_change
    def confirm_trial(self, now):
        if not self.pending:
            return False
        if now >= self.pending["deadline"]:
            self.revert_trial()
            return False
        self.profile = self.known_good = self.pending["profile"]
        self.pending = None
        self.failures = 0
        self.save()
        return True

    @durable_change
    def revert_trial(self):
        self.pending = None
        self.save()
        return self.profile

    @durable_change
    def record_failure(self):
        self.failures = min(3, self.failures + 1)
        if self.recovery_required:
            self.profile = self.known_good
            self.pending = None
        self.save()
        return self.failures

    @durable_change
    def record_success(self):
        self.failures = 0
        self.save()

    @durable_change
    def restore_defaults(self):
        """Preserve the old private file, then reset app settings only."""
        if self.path.is_symlink():
            raise ValueError("Configuration became a symlink")
        raw = self.path.read_bytes() if self.path.exists() else None
        digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
        if digest != self._disk_hash:
            raise RuntimeError("Configuration changed in another process")
        backup = None
        if raw is not None:
            backup = self.path.with_name(self.path.name + ".before-reset-" + digest[:16])
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                if backup.is_symlink() or not backup.is_file() or backup.read_bytes() != raw:
                    raise RuntimeError("Existing recovery backup differs; preserving both files") from None
            else:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
        self.profile = self.known_good = Profile()
        self.pending = None
        self.failures = 0
        self.config_error = None
        self.recovered_trial = False
        self.save()
        return backup
