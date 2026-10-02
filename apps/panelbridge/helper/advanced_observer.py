"""Read-only Linux observations for the advanced helper's trusted boundary.

No observation grants readiness or changes firmware, clocks, files or services.
Production composition must keep the observer and its I/O object root-private.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import time

from .advanced import BootFacts, HealthSample, LinuxControls
from .network import Budget, HelperError

DT = "/sys/firmware/devicetree/base"
BOOT = "/boot/firmware"
BOOT_ID = "/proc/sys/kernel/random/boot_id"
VCGENCMD = "/usr/bin/vcgencmd"
COMMANDS = frozenset({(VCGENCMD, "get_config", "int"),
                      (VCGENCMD, "get_config", "boot_ramdisk"),
                      (VCGENCMD, "get_config", "tryboot_a_b"),
                      (VCGENCMD, "get_throttled")})
ASSETS = ("autoboot.txt", "boot.img", "tryboot.img", "boot.sig", "tryboot.sig")
READ_PATHS = frozenset({
    BOOT_ID, "/proc/self/mountinfo", "/proc/sys/kernel/osrelease", "/proc/version",
    "/usr/lib/os-release", "/sys/class/thermal/thermal_zone0/temp",
    "/sys/class/thermal/thermal_zone0/type", BOOT + "/config.txt",
    *(DT + "/" + name for name in ("model", "compatible", "serial-number", "system/linux,revision")),
    *(DT + "/chosen/bootloader/" + name for name in
      ("tryboot", "signed", "partition", "boot-mode", "version", "build_timestamp")),
})
KERNEL_FAULT = re.compile(
    rb"(?:\bI/O error\b|\bBuffer I/O\b|\bEXT4-fs error\b|\bFAT-fs.*error\b|"
    rb"\bBTRFS.*(?:error|corrupt)\b|\bSQUASHFS error\b|\bmemory corruption\b|"
    rb"\bSError\b|\bHardware Error\b|\bKernel panic\b|\bOops:|\bBUG:|"
    rb"\b(?:soft|hard) lockup\b|\b(?:mmc|nvme).*\b(?:error|timeout)\b)", re.I)
KERNEL_COVERAGE_LOST = re.compile(
    rb"(?:\b(?:messages|callbacks) suppressed\b|\bmessages (?:lost|dropped)\b|"
    rb"\b(?:lost|dropped) [0-9]+ (?:kernel )?messages\b)", re.I)


def _unavailable(code="ObservationUnavailable"):
    raise HelperError(code, "Trusted Linux observation is missing, ambiguous or outside its bounds")


class LinuxReadOnlyIO:
    """Fixed reads and commands. Inject a fixture object only in trusted code."""
    @staticmethod
    def _root():
        if os.geteuid() != 0:
            _unavailable()

    def read(self, path, limit):
        self._root()
        if path not in READ_PATHS and not re.fullmatch(r"/sys/dev/block/[0-9]+:[0-9]+/partition", path):
            _unavailable()
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                _unavailable()
            value = stream.read(limit + 1)
        if len(value) > limit:
            _unavailable()
        return value

    def exists(self, path):
        self._root()
        if path not in {BOOT + "/" + name for name in ASSETS}:
            _unavailable()
        try:
            os.lstat(path)
            return True  # A symlink or directory is also an unsupported asset.
        except FileNotFoundError:
            return False

    def link(self, path):
        self._root()
        if not re.fullmatch(r"/sys/dev/block/[0-9]+:[0-9]+", path):
            _unavailable()
        result = str(Path(path).resolve(strict=True))
        if not result.startswith("/sys/devices/"):
            _unavailable()
        return result

    def command(self, argv, timeout, limit):
        self._root()
        if tuple(argv) not in COMMANDS or not 0 < timeout <= 2 or not 0 < limit <= 65536:
            _unavailable()
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, close_fds=True,
                                 env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
        deadline = time.monotonic() + timeout
        chunks, total = [], 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or not selector.select(remaining):
                        _unavailable()
                    chunk = os.read(child.stdout.fileno(), min(4096, limit + 1 - total))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > limit:
                        _unavailable()
            if child.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
                _unavailable()
            return b"".join(chunks)
        finally:
            if child.poll() is None:
                child.kill()
            try:
                child.wait(timeout=.2)
            except subprocess.TimeoutExpired:
                pass
            child.stdout.close()

    def kernel_records(self, budget):
        self._root()
        fd = os.open("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_uid != 0:
                _unavailable("HealthUnavailable")
            records, total = [], 0
            while len(records) < 16384:
                budget.check()
                try:
                    record = os.read(fd, 16384)
                except BlockingIOError:
                    return records
                if not record:
                    _unavailable("HealthUnavailable")
                records.append(record)
                total += len(record)
                if total > 4 * 1024 * 1024:
                    _unavailable("HealthUnavailable")
            _unavailable("HealthUnavailable")
        finally:
            os.close(fd)


def _text(raw):
    if not isinstance(raw, bytes) or b"\x00" in raw:
        _unavailable()
    return raw.decode("ascii").strip()


def _dt_string(raw):
    if not isinstance(raw, bytes) or not raw.endswith(b"\0") or b"\0" in raw[:-1]:
        _unavailable()
    return _text(raw[:-1])


def _u32(raw):
    if not isinstance(raw, bytes) or len(raw) != 4:
        _unavailable()
    return int.from_bytes(raw, "big")


def _integers(raw):
    values = {}
    for line in _text(raw).splitlines():
        match = re.fullmatch(r"([a-z][a-z0-9_]*(?::[0-9]+)?)=(-?(?:[0-9]+|0x[0-9a-fA-F]+))", line)
        if not match or match[1] in values:
            _unavailable()
        values[match[1]] = int(match[2], 16 if "0x" in match[2] else 10)
    return values


def _mounts(raw):
    result = []
    for line in _text(raw).splitlines():
        fields = line.split()
        if len(fields) < 10 or fields.count("-") != 1:
            _unavailable()
        separator = fields.index("-")
        if separator < 6 or len(fields) != separator + 4 or not re.fullmatch(r"[0-9]+:[0-9]+", fields[2]):
            _unavailable()
        result.append({"device": fields[2], "root": fields[3], "target": fields[4],
                       "options": fields[5].split(","), "fs": fields[separator + 1],
                       "source": fields[separator + 2], "super": fields[separator + 3].split(",")})
    if len(result) > 512:
        _unavailable()
    return result


class LinuxAdvancedObserver(LinuxControls):
    """LinuxControls plus bounded observations; no readiness is enrolled here."""
    def __init__(self, *, io=None, clock=time.monotonic):
        super().__init__()
        self.io, self.clock = io or LinuxReadOnlyIO(), clock

    def _read(self, path, budget, limit=65536):
        budget.check()
        value = self.io.read(path, limit)
        budget.check()
        if not isinstance(value, bytes) or len(value) > limit:
            _unavailable()
        return value

    def _command(self, argv, budget):
        budget.check()
        value = self.io.command(argv, budget.timeout_ms(2000) / 1000, 65536)
        budget.check()
        if not isinstance(value, bytes) or len(value) > 65536:
            _unavailable()
        return value

    def _boot_id(self, budget):
        value = _text(self._read(BOOT_ID, budget, 64))
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value):
            _unavailable()
        return value

    def _flag(self, name, budget):
        try:
            values = _integers(self._command((VCGENCMD, "get_config", name), budget))
            if set(values) != {name} or values[name] not in (0, 1):
                return None
            return bool(values[name])
        except (OSError, ValueError, HelperError, subprocess.SubprocessError):
            return None

    def _layout(self, budget, partition, mode):
        mounts = _mounts(self._read("/proc/self/mountinfo", budget))
        roots = [row for row in mounts if row["target"] == "/"]
        boots = [row for row in mounts if row["target"] == BOOT]
        if len(roots) != 1 or len(boots) != 1 or partition != 1 or mode not in (1, 4, 6):
            _unavailable()
        root, boot = roots[0], boots[0]
        for row in (root, boot):
            if (row["root"] != "/" or "rw" not in row["options"] or "rw" not in row["super"]
                    or not row["source"].startswith("/dev/")
                    or sum(other["device"] == row["device"] for other in mounts) != 1):
                _unavailable()
        if boot["fs"] != "vfat" or root["fs"] != "ext4" or root["device"] == boot["device"]:
            _unavailable()
        parents = []
        for row, number in ((boot, 1), (root, 2)):
            device = "/sys/dev/block/" + row["device"]
            if _text(self._read(device + "/partition", budget, 16)) != str(number):
                _unavailable()
            resolved = self.io.link(device)
            if not isinstance(resolved, str) or not resolved.startswith("/sys/devices/"):
                _unavailable()
            if Path(resolved).name != Path(row["source"]).name:
                _unavailable()
            parents.append(str(Path(resolved).parent))
        if parents[0] != parents[1]:
            _unavailable()
        disk = Path(parents[0]).name
        if not re.fullmatch({1: r"mmcblk[0-9]+", 4: r"sd[a-z]+", 6: r"nvme[0-9]+n[0-9]+"}[mode], disk):
            _unavailable()
        if any(self.io.exists(BOOT + "/" + asset) for asset in ASSETS):
            _unavailable()
        config = self._read(BOOT + "/config.txt", budget)
        text = _text(config)
        if not text:
            _unavailable()
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip().lower()
            if (line.startswith("include") or line.startswith(("[tryboot", "[partition", "[boot_partition"))
                    or re.match(r"(?:boot_ramdisk|tryboot_a_b|boot_partition|os_prefix)\s*=", line)):
                _unavailable()
        return {"disk_path": parents[0], "boot_source": boot["source"],
                "root_source": root["source"], "config_sha256": hashlib.sha256(config).hexdigest(),
                "partition": partition, "mode": mode}

    def observe_boot(self):
        budget = Budget(self.clock() + 8, clock=self.clock)
        try:
            boot_id = self._boot_id(budget)
            model = _dt_string(self._read(DT + "/model", budget, 128))
            compatible = self._read(DT + "/compatible", budget, 256).split(b"\0")
            if (not re.fullmatch(r"Raspberry Pi 5 Model B Rev [0-9]+\.[0-9]+", model)
                    or b"raspberrypi,5-model-b" not in compatible or b"brcm,bcm2712" not in compatible
                    or compatible[-1] != b""):
                _unavailable()
            tryboot = _u32(self._read(DT + "/chosen/bootloader/tryboot", budget, 4))
            if tryboot not in (0, 1):
                _unavailable()
            configured = _integers(self._command((VCGENCMD, "get_config", "int"), budget)).get("arm_freq")
            if type(configured) is not int or not 100 <= configured <= 5000:
                _unavailable()
            secure = None
            try:
                secure = bool(_u32(self._read(DT + "/chosen/bootloader/signed", budget, 4)))
            except (OSError, ValueError, HelperError):
                pass
            ramdisk, ab = self._flag("boot_ramdisk", budget), self._flag("tryboot_a_b", budget)
            platform_id, verified = "", False
            try:
                prefix = DT + "/chosen/bootloader/"
                layout = self._layout(budget, _u32(self._read(prefix + "partition", budget, 4)),
                                      _u32(self._read(prefix + "boot-mode", budget, 4)))
                serial = _dt_string(self._read(DT + "/serial-number", budget, 128))
                version = _dt_string(self._read(prefix + "version", budget, 128))
                if not re.fullmatch(r"[0-9a-fA-F]{16}", serial) or not re.fullmatch(r"[0-9a-fA-F]{40}", version):
                    _unavailable()
                identity = {"model": model, "serial": serial,
                            "revision": _u32(self._read(DT + "/system/linux,revision", budget, 4)),
                            "firmware_version": version,
                            "firmware_build": _u32(self._read(prefix + "build_timestamp", budget, 4)),
                            "layout": layout}
                for key, path in (("kernel_release", "/proc/sys/kernel/osrelease"),
                                  ("kernel_version", "/proc/version"), ("os", "/usr/lib/os-release")):
                    value = _text(self._read(path, budget))
                    if not value:
                        _unavailable()
                    identity[key] = value
                platform_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                verified = secure is False and ramdisk is False and ab is False
            except (OSError, ValueError, HelperError):
                pass
            if self._boot_id(budget) != boot_id:
                _unavailable()
            budget.check()
            return BootFacts(boot_id, model, configured, bool(tryboot), verified, secure, ramdisk, ab, platform_id)
        except (OSError, ValueError, HelperError, subprocess.SubprocessError):
            _unavailable("BootObservationUnavailable")

    def health(self):
        started = self.clock()
        budget = Budget(started + 6, clock=self.clock)
        try:
            boot_id = self._boot_id(budget)
            if _text(self._read("/sys/class/thermal/thermal_zone0/type", budget, 64)) != "cpu-thermal":
                _unavailable()
            raw = _text(self._read("/sys/class/thermal/thermal_zone0/temp", budget, 32))
            if not re.fullmatch(r"-?[0-9]{1,6}", raw) or not -20000 <= int(raw) <= 150000:
                _unavailable()
            temperature = int(raw) / 1000
            match = re.fullmatch(r"throttled=0x([0-9a-fA-F]{1,8})", _text(self._command((VCGENCMD, "get_throttled"), budget)))
            if not match or int(match[1], 16) & ~0xF000F:
                _unavailable()
            faults, marker, previous_time = False, False, -1
            records = self.io.kernel_records(budget)
            if not isinstance(records, list) or not 1 <= len(records) <= 16384:
                _unavailable()
            total = 0
            for expected, record in enumerate(records):
                budget.check()
                if not isinstance(record, bytes) or len(record) > 16384:
                    _unavailable()
                total += len(record)
                if total > 4 * 1024 * 1024:
                    _unavailable()
                header, message = record.split(b";", 1)
                fields = header.split(b",")
                if (len(fields) < 4 or fields[3] != b"-"
                        or not all(re.fullmatch(rb"[0-9]+", value) for value in fields[:3])):
                    _unavailable()
                priority, sequence, timestamp = map(int, fields[:3])
                if (sequence != expected or not 0 <= priority <= 2047 or timestamp < previous_time
                        or timestamp > self.clock() * 1_000_000 or not record.endswith(b"\n")):
                    _unavailable()
                previous_time = timestamp
                if priority >> 3 == 0:
                    if KERNEL_COVERAGE_LOST.search(message):
                        _unavailable()
                    marker = marker or (expected < 32 and b"Linux version " in message and timestamp <= 2_000_000)
                    faults = faults or priority & 7 <= 3 or bool(KERNEL_FAULT.search(message))
            if not marker or self._boot_id(budget) != boot_id:
                _unavailable()
            budget.check()
            return HealthSample(started, temperature, int(match[1], 16), faults)
        except (OSError, ValueError, HelperError, subprocess.SubprocessError):
            _unavailable("HealthUnavailable")
