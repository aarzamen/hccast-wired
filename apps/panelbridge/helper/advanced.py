"""Fixed-purpose advanced operations; no D-Bus authority is added here.

The integrator supplies trusted boot/health observations, never client claims.
Only the CPU backend and its two fixed reboot forms cross the hardware boundary.
The normal boot configuration is never modified. See interfaces/ADVANCED.md.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time
import uuid

from .network import HelperError

STOCK_MHZ = 2400
MAX_MHZ = 2700
CONFIRM_SECONDS = 120
MAX_TRIAL_SECONDS = 4 * 60 * 60
JOURNAL = "advanced.json"
READINESS = "advanced-readiness.json"
BASELINE = "PANELBRIDGE_BASELINE.txt"
SAFE_MODE = "PANELBRIDGE_SAFE_MODE"
GOVERNORS = frozenset({"ondemand", "schedutil", "conservative", "powersave", "performance"})
CPU_FIELDS = {"governor": "scaling_governor", "max_khz": "scaling_max_freq"}


@dataclass(frozen=True)
class BootFacts:
    boot_id: str
    model: str
    configured_mhz: int
    tryboot: bool
    layout_verified: bool = False
    secure_boot: bool | None = None
    boot_ramdisk: bool | None = None
    ab_boot: bool | None = None
    platform_id: str = ""


@dataclass(frozen=True)
class HealthSample:
    monotonic_s: float
    temperature_c: float
    throttled_bits: int
    data_errors: bool


def _fail(code, message):
    raise HelperError(code, message)


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


class _Directory:
    """Pinned no-follow directory; ancestors cannot confer untrusted authority."""
    def __init__(self, path, uid):
        path = Path(path)
        if not path.is_absolute() or ".." in path.parts:
            _fail("UnsafePath", "Advanced paths must be fixed absolute directories")
        self.uid = uid
        self.fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in path.parts[1:]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                  dir_fd=self.fd)
                os.close(self.fd)
                self.fd = next_fd
                info = os.fstat(self.fd)
                if info.st_uid not in {0, uid} or info.st_mode & 0o022:
                    _fail("UnsafePath", "Advanced directory has unsafe ownership or permissions")
            if os.fstat(self.fd).st_uid != uid:
                _fail("UnsafePath", "Advanced directory has the wrong owner")
        except Exception:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    def _check(self, fd):
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid
                or info.st_mode & 0o022 or info.st_nlink != 1):
            _fail("UnsafeFile", "Advanced files must be owned regular files with one link")

    def read(self, name, *, missing=None):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self.fd)
        except FileNotFoundError:
            return missing
        with os.fdopen(fd, "rb") as stream:
            self._check(stream.fileno())
            raw = stream.read(262145)
        if len(raw) > 262144:
            _fail("UnsafeFile", "Advanced file exceeds its size limit")
        return raw

    def write(self, name, raw, *, expected):
        if self.read(name) != expected:
            _fail("Conflict", "Advanced file was changed outside this transaction")
        temporary = ".panelbridge-" + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if self.read(name) != expected:
                _fail("Conflict", "Advanced file changed during the transaction")
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def remove(self, name, expected):
        current = self.read(name)
        if current is None:
            return
        if current != expected:
            _fail("Conflict", "Refusing to remove a changed boot file")
        os.unlink(name, dir_fd=self.fd)
        os.fsync(self.fd)

    def json(self, name):
        raw = self.read(name)
        if raw is None:
            return None
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            _fail("InvalidState", "Advanced record is malformed")
        if not isinstance(value, dict) or value.get("api_version") != 1:
            _fail("InvalidState", "Advanced record has an unsupported schema")
        return value


class LinuxControls:
    """Fixed sysfs policy0 controls and fixed reboot argv, for a trusted adapter.

    A caller must supply observe_boot() and health() in its adapter. This class
    does not infer physical readiness or expose arbitrary sysfs paths to clients.
    """
    def __init__(self, *, cpu_path=Path("/sys/devices/system/cpu/cpufreq/policy0"),
                 trusted_uid=0):
        self.cpu_path, self.uid = cpu_path, trusted_uid

    def cpu_snapshot(self):
        directory = _Directory(self.cpu_path, self.uid)
        try:
            def read(name):
                raw = directory.read(name)
                if raw is None:
                    _fail("CPUUnavailable", "CPU policy attributes are unavailable")
                return raw.decode("ascii").strip()
            return {"governor": read("scaling_governor"),
                    "governors": read("scaling_available_governors").split(),
                    "min_khz": int(read("scaling_min_freq")),
                    "max_khz": int(read("scaling_max_freq")),
                    "hardware_max_khz": int(read("cpuinfo_max_freq"))}
        except (ValueError, UnicodeError):
            _fail("CPUUnavailable", "CPU policy attributes are malformed")
        finally:
            directory.close()

    def write_cpu(self, field, value):
        if field not in CPU_FIELDS or (field == "governor" and value not in GOVERNORS):
            _fail("InvalidCPU", "Unsupported CPU policy field or governor")
        if field == "max_khz" and (type(value) is not int or not 1 <= value <= STOCK_MHZ * 1000):
            _fail("InvalidCPU", "CPU cap must not exceed the stock frequency")
        directory = _Directory(self.cpu_path, self.uid)
        try:
            fd = os.open(CPU_FIELDS[field], os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=directory.fd)
            with os.fdopen(fd, "wb") as stream:
                directory._check(stream.fileno())
                stream.write((str(value) + "\n").encode("ascii"))
        finally:
            directory.close()

    def reboot(self, *, trial):
        if type(trial) is not bool:
            _fail("InvalidReboot", "Only normal or tryboot reboot is supported")
        argv = ["/usr/sbin/reboot", "0 tryboot"] if trial else ["/usr/sbin/reboot"]
        try:
            subprocess.run(argv, check=True, timeout=10, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"})
        except (OSError, subprocess.SubprocessError):
            _fail("RebootFailed", "Reboot request failed; restoration record is retained")


class AdvancedHelper:
    def __init__(self, backend, *, boot=Path("/boot/firmware"),
                 state=Path("/var/lib/panelbridge"), trusted_uid=0, clock=time.monotonic):
        self.backend, self.boot, self.state = backend, boot, state
        self.uid, self.clock = trusted_uid, clock

    @contextmanager
    def _transaction(self):
        directories = []
        lock_fd = None
        try:
            state = _Directory(self.state, self.uid)
            directories.append(state)
            lock_fd = os.open("advanced.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                              0o600, dir_fd=state.fd)
            state._check(lock_fd)
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            boot = _Directory(self.boot, self.uid)
            directories.append(boot)
            yield state, boot
        except BlockingIOError:
            _fail("Busy", "Another advanced transaction is in progress")
        except OSError:
            _fail("AdvancedIO", "Advanced operation failed; restoration record is retained")
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
            for directory in reversed(directories):
                directory.close()

    @staticmethod
    def _load(state):
        state.journal_raw = state.read(JOURNAL)
        record = state.json(JOURNAL) or {"api_version": 1}
        if set(record) - {"api_version", "cpu", "trial", "profile"}:
            _fail("InvalidState", "Advanced journal contains unknown fields")
        def require(condition):
            if not condition:
                _fail("InvalidState", "Advanced journal is incomplete or inconsistent")
        def token(value):
            return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9-]{1,64}", value)
        def digest(value):
            return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
        def number(value):
            return type(value) in (int, float) and math.isfinite(value) and value >= 0
        try:
            for kind in ("cpu", "trial", "profile"):
                if kind in record:
                    require(isinstance(record[kind], dict))
            if "cpu" in record:
                cpu = record["cpu"]
                require(cpu["state"] in {"applying", "applied", "restored"} and token(cpu["boot_id"]))
                for key in ("baseline", "before", "target"):
                    values = cpu[key]
                    require(isinstance(values, dict) and set(values) == set(CPU_FIELDS))
                    require(values["governor"] in GOVERNORS and type(values["max_khz"]) is int
                            and 0 < values["max_khz"] <= STOCK_MHZ * 1000)
            if "profile" in record:
                profile = record["profile"]
                require(type(profile["mhz"]) is int and STOCK_MHZ < profile["mhz"] <= MAX_MHZ
                        and digest(profile["config_sha256"]) and token(profile["trial_id"])
                        and isinstance(profile["platform_id"], str) and bool(profile["platform_id"]))
            if "trial" in record:
                trial = record["trial"]
                require(trial["state"] in {"staging", "requested", "active", "confirmed",
                                           "rollback_requested", "manual_recovery_required", "restored"}
                        and token(trial["id"]) and token(trial["origin_boot_id"])
                        and type(trial["mhz"]) is int and STOCK_MHZ < trial["mhz"] <= MAX_MHZ
                        and trial["baseline_bits"] == 0 and type(trial["baseline_bits"]) is int
                        and isinstance(trial["platform_id"], str) and bool(trial["platform_id"]))
                require(type(trial.get("cleanup_conflict", False)) is bool)
                baseline, candidate = trial["baseline"].encode("ascii"), trial["candidate"].encode("ascii")
                require(len(baseline) <= 65536 and _digest(baseline) == trial["config_sha256"]
                        and _digest(candidate) == trial["candidate_sha256"]
                        and candidate == baseline + ("\n# PanelBridge trial; normal config is unchanged\n"
                                                     f"[all]\narm_freq={trial['mhz']}\n").encode())
                if trial["state"] == "requested":
                    require(number(trial["requested_s"]))
                if trial["state"] in {"active", "confirmed"}:
                    require(token(trial["active_boot_id"]) and number(trial["started_s"])
                            and number(trial["deadline_s"])
                            and trial["deadline_s"] == trial["started_s"] + CONFIRM_SECONDS)
        except (KeyError, TypeError, ValueError, AttributeError):
            _fail("InvalidState", "Advanced journal is incomplete or inconsistent")
        return record

    @staticmethod
    def _save(state, record):
        raw = (json.dumps(record, allow_nan=False, sort_keys=True) + "\n").encode()
        if len(raw) > 262144:
            _fail("InvalidState", "Advanced journal exceeds its size limit")
        state.write(JOURNAL, raw, expected=state.journal_raw)
        state.journal_raw = raw

    def _health(self, baseline_bits=0):
        sample = self.backend.health()
        valid = (isinstance(sample, HealthSample)
                 and type(sample.monotonic_s) in (int, float) and math.isfinite(sample.monotonic_s)
                 and 0 <= self.clock() - sample.monotonic_s <= 10
                 and type(sample.temperature_c) in (int, float) and math.isfinite(sample.temperature_c)
                 and -20 <= sample.temperature_c < 70
                 and type(sample.throttled_bits) is int and 0 <= sample.throttled_bits <= 0xF000F
                 and not sample.throttled_bits & ~0xF000F
                 and not sample.throttled_bits & 0xF
                 and not sample.throttled_bits & ~baseline_bits
                 and sample.data_errors is False)
        if not valid:
            _fail("Unhealthy", "Fresh health below 70 C with no new faults is required")
        return sample

    def _facts(self):
        facts = self.backend.observe_boot()
        if (not isinstance(facts, BootFacts) or not isinstance(facts.boot_id, str)
                or not re.fullmatch(r"[A-Za-z0-9-]{1,64}", facts.boot_id)
                or not facts.model.startswith("Raspberry Pi 5 Model B")
                or type(facts.tryboot) is not bool
                or type(facts.configured_mhz) is not int):
            _fail("UnsupportedHost", "Verified Pi 5 model and current boot identity are required")
        return facts

    @staticmethod
    def _cpu_values(cpu):
        try:
            governor, low, high, maximum = (cpu[key] for key in
                ("governor", "min_khz", "max_khz", "hardware_max_khz"))
            valid = (governor in GOVERNORS and governor in cpu["governors"]
                     and all(type(value) is int for value in (low, high, maximum))
                     and 0 < low <= high <= maximum <= STOCK_MHZ * 1000)
        except (KeyError, TypeError):
            valid = False
        if not valid:
            _fail("CPUUnavailable", "A supported stock CPU policy is required")
        return {"governor": governor, "max_khz": high}

    def _restore_cpu(self, state, record):
        cpu = record.get("cpu")
        if not cpu or cpu["state"] == "restored":
            return
        facts = self._facts()
        if facts.boot_id != cpu["boot_id"]:
            # Runtime cpufreq writes do not survive reboot. Do not overwrite new policy.
            cpu["state"] = "restored"
            self._save(state, record)
            return
        current = self._cpu_values(self.backend.cpu_snapshot())
        for field in CPU_FIELDS:
            if current[field] not in (cpu["before"][field], cpu["target"][field], cpu["baseline"][field]):
                _fail("Conflict", "CPU policy changed outside the app; preserving current values")
        for field in ("max_khz", "governor"):
            if current[field] != cpu["baseline"][field]:
                self.backend.write_cpu(field, cpu["baseline"][field])
        if self._cpu_values(self.backend.cpu_snapshot()) != cpu["baseline"]:
            _fail("CPUVerifyFailed", "CPU baseline restoration did not read back correctly")
        cpu["state"] = "restored"
        self._save(state, record)

    def set_cpu(self, governor, cap_mhz):
        with self._transaction() as (state, boot):
            facts, record = self._facts(), self._load(state)
            if facts.tryboot or facts.configured_mhz != STOCK_MHZ or boot.read(SAFE_MODE) is not None:
                _fail("Unavailable", "CPU tuning requires a normal stock boot outside safe mode")
            if record.get("trial", {}).get("state", "restored") != "restored":
                _fail("Busy", "Finish boot trial restoration before CPU tuning")
            snapshot = self.backend.cpu_snapshot()
            current = self._cpu_values(snapshot)
            if (not isinstance(governor, str) or governor not in GOVERNORS
                    or governor not in snapshot["governors"] or type(cap_mhz) is not int
                    or not snapshot["min_khz"] <= cap_mhz * 1000 <= snapshot["hardware_max_khz"]):
                _fail("InvalidCPU", "Select an available governor and supported stock CPU cap")
            old = record.get("cpu")
            if old and old["state"] != "restored" and old["boot_id"] == facts.boot_id:
                if old["state"] != "applied" or current != old["target"]:
                    _fail("Conflict", "Restore the pending or externally changed CPU transaction first")
                baseline = old["baseline"]
            else:
                baseline = current
            record["cpu"] = {"state": "applying", "boot_id": facts.boot_id, "baseline": baseline,
                             "before": current, "target": {"governor": governor, "max_khz": cap_mhz * 1000}}
            self._save(state, record)
            try:
                for field in ("max_khz", "governor"):
                    self.backend.write_cpu(field, record["cpu"]["target"][field])
                if self._cpu_values(self.backend.cpu_snapshot()) != record["cpu"]["target"]:
                    _fail("CPUVerifyFailed", "CPU settings did not read back correctly")
            except (OSError, HelperError):
                self._restore_cpu(state, record)
                _fail("CPUApplyFailed", "CPU setting failed and the saved baseline was restored")
            record["cpu"]["state"] = "applied"
            self._save(state, record)
            return {"cpu_restored": False, **record["cpu"]["target"]}

    def restore_cpu(self):
        with self._transaction() as (state, _):
            self._restore_cpu(state, self._load(state))
            return {"cpu_restored": True}

    @staticmethod
    def _config(boot, facts):
        if (facts.layout_verified is not True or facts.secure_boot is not False
                or facts.boot_ramdisk is not False or facts.ab_boot is not False):
            _fail("UnsupportedLayout", "Verified simple non-secure boot layout is required")
        for name in ("autoboot.txt", "boot.img", "tryboot.img", "boot.sig", "tryboot.sig"):
            if boot.read(name) is not None:
                _fail("UnsupportedLayout", "Existing alternate boot assets require a separate recovery design")
        raw = boot.read("config.txt")
        if raw is None or len(raw) > 65536:
            _fail("UnsupportedLayout", "Normal boot configuration is missing or too large")
        try:
            text = raw.decode("ascii")
        except UnicodeError:
            _fail("UnsupportedLayout", "Only plain ASCII boot configuration is supported")
        forbidden = ("over_voltage", "force_turbo", "temp_", "sdram_", "gpu_", "core_freq",
                     "h264_freq", "isp_freq", "v3d_freq", "hevc_freq", "program_", "otp",
                     "boot_ramdisk", "tryboot", "boot_partition", "os_prefix", "arm_freq_min",
                     "initial_turbo", "kernel_watchdog", "set_reboot", "eeprom", "erase_eeprom")
        for line in text.splitlines():
            line = line.split("#", 1)[0].strip().lower()
            key = line.split("=", 1)[0].strip()
            if (line.startswith("include") or key.startswith(forbidden)
                    or line.startswith(("[tryboot", "[partition", "[boot_partition"))
                    or (key == "arm_freq" and line != "arm_freq=2400") or "\x00" in line):
                _fail("UnsupportedLayout", "Boot configuration contains unsupported trial or tuning directives")
        return raw

    def start_trial(self, mhz):
        with self._transaction() as (state, boot):
            facts, record = self._facts(), self._load(state)
            if facts.tryboot or facts.configured_mhz != STOCK_MHZ:
                _fail("StockBaselineRequired", "Begin each trial from a verified normal stock boot")
            if boot.read(SAFE_MODE) is not None:
                _fail("SafeMode", "Remove the boot safe-mode flag before starting a trial")
            if record.get("trial", {}).get("state", "restored") != "restored":
                _fail("Busy", "An unfinished boot transaction must be restored first")
            if record.get("cpu", {}).get("state", "restored") != "restored":
                _fail("StockBaselineRequired", "Restore runtime CPU policy before a boot trial")
            baseline = self._config(boot, facts)
            config_hash = _digest(baseline)
            readiness = state.json(READINESS)
            required = ("cooling_verified", "supply_verified", "recovery_verified", "stock_load_verified",
                        "backup_verified", "semantics_reviewed")
            if (not readiness or any(readiness.get(key) is not True for key in required)
                    or readiness.get("boot_id") != facts.boot_id
                    or readiness.get("platform_id") != facts.platform_id or not facts.platform_id
                    or readiness.get("config_sha256") != config_hash):
                _fail("ReadinessRequired", "Trusted cooling, supply, stock-load and recovery evidence is required")
            cpu = self._cpu_values(self.backend.cpu_snapshot())
            if cpu["max_khz"] != STOCK_MHZ * 1000:
                _fail("StockBaselineRequired", "Current CPU cap must match the measured stock baseline")
            self._health()
            profile = record.get("profile", {})
            previous = (profile.get("mhz", STOCK_MHZ) if profile.get("config_sha256") == config_hash
                        and profile.get("platform_id") == facts.platform_id else STOCK_MHZ)
            if type(mhz) is not int or not STOCK_MHZ < mhz <= min(MAX_MHZ, previous + 50):
                _fail("InvalidFrequency", "Use at most 50 MHz above the last confirmed step, up to 2700 MHz")
            if boot.read("tryboot.txt") is not None:
                _fail("Conflict", "Existing tryboot configuration is not owned by this transaction")
            existing_baseline = boot.read(BASELINE)
            if existing_baseline is not None and (existing_baseline != baseline
                    or record.get("trial", {}).get("baseline") != baseline.decode()):
                _fail("Conflict", "Boot baseline changed; preserve and review it before another trial")
            candidate = baseline + ("\n# PanelBridge trial; normal config is unchanged\n[all]\n"
                                    f"arm_freq={mhz}\n").encode()
            record["trial"] = {"id": uuid.uuid4().hex, "state": "staging", "origin_boot_id": facts.boot_id,
                               "mhz": mhz, "baseline": baseline.decode(), "candidate": candidate.decode(),
                               "config_sha256": config_hash, "candidate_sha256": _digest(candidate),
                               "platform_id": facts.platform_id, "baseline_bits": 0}
            self._save(state, record)
            if existing_baseline is None:
                boot.write(BASELINE, baseline, expected=None)
            boot.write("tryboot.txt", candidate, expected=None)
            if boot.read("config.txt") != baseline:
                _fail("Conflict", "Normal configuration changed during trial staging; reboot cancelled")
            self._health()
            if self._facts() != facts:
                _fail("Conflict", "Boot facts changed during trial staging; reboot cancelled")
            record["trial"]["state"] = "requested"
            record["trial"]["requested_s"] = self.clock()
            self._save(state, record)
            self.backend.reboot(trial=True)
            return self._result(record)

    @staticmethod
    def _result(record):
        trial = record.get("trial", {})
        return {"state": trial.get("state", "restored"), "trial_id": trial.get("id"),
                "mhz": trial.get("mhz"), "reason": trial.get("reason"),
                "normal_reboot_required": trial.get("state") == "rollback_requested",
                "manual_recovery_required": trial.get("state") == "manual_recovery_required",
                "cleanup_conflict": trial.get("cleanup_conflict", False)}

    @staticmethod
    def _matches(boot, name, expected):
        try:
            return boot.read(name) == expected
        except (HelperError, OSError):
            return False

    def _normal_baseline_verified(self, boot, facts, trial):
        if facts.platform_id != trial["platform_id"]:
            return False
        try:
            return self._config(boot, facts) == trial["baseline"].encode()
        except (HelperError, OSError):
            return False

    def _manual_recovery(self, state, record):
        record["trial"].update(state="manual_recovery_required", reason="normal-baseline-unverified")
        self._save(state, record)
        return self._result(record)

    def _rollback(self, state, boot, record, facts, reason):
        self._restore_cpu(state, record)
        trial = record.get("trial")
        if not trial:
            if facts.tryboot:
                _fail("UnmanagedTrial", "Current tryboot has no app restoration record")
            return self._result(record)
        # Cleanup ownership and safe retreat are independent. Preserve an outside
        # edit while still escaping a trial if its normal baseline is proven.
        trial["cleanup_conflict"] = False
        try:
            boot.remove("tryboot.txt", trial["candidate"].encode())
        except (HelperError, OSError):
            trial["cleanup_conflict"] = True
        if not self._normal_baseline_verified(boot, facts, trial):
            return self._manual_recovery(state, record)
        if (facts.tryboot or (facts.boot_id == trial["origin_boot_id"]
                              and trial["state"] != "staging")):
            trial["state"], trial["reason"] = "rollback_requested", reason
            request = trial.get("reboot_requested_boot_id") != facts.boot_id
            trial["reboot_requested_boot_id"] = facts.boot_id
            self._save(state, record)
            if request:
                if self._facts() != facts or not self._normal_baseline_verified(boot, facts, trial):
                    # No reboot was issued: permit a new bounded attempt only
                    # after manual restoration makes the baseline verifiable.
                    trial.pop("reboot_requested_boot_id", None)
                    return self._manual_recovery(state, record)
                self.backend.reboot(trial=False)
        else:
            if facts.configured_mhz != STOCK_MHZ:
                _fail("BaselineUnverified", "Verify a new normal stock boot before completing restoration")
            self._health(trial["baseline_bits"])
            trial["state"], trial["reason"] = "restored", reason
            self._save(state, record)
        return self._result(record)

    def rollback(self):
        with self._transaction() as (state, boot):
            return self._rollback(state, boot, self._load(state), self._facts(), "requested")

    def guardian(self):
        """Call at early service start and at least every five seconds thereafter."""
        with self._transaction() as (state, boot):
            facts, record = self._facts(), self._load(state)
            trial = record.get("trial")
            safe = boot.read(SAFE_MODE) is not None
            if safe:
                return self._rollback(state, boot, record, facts, "safe-mode")
            if trial and trial["state"] == "restored" and trial.get("cleanup_conflict"):
                return self._rollback(state, boot, record, facts, "cleanup-conflict")
            if not trial or trial["state"] == "restored":
                if facts.tryboot:
                    _fail("UnmanagedTrial", "Current tryboot has no active app restoration record")
                # CPU intent left incomplete by a crash is recoverable on startup.
                if record.get("cpu", {}).get("state") == "applying":
                    self._restore_cpu(state, record)
                return self._result(record)
            if not self._normal_baseline_verified(boot, facts, trial):
                return self._rollback(state, boot, record, facts, "normal-baseline-unverified")
            if (trial["state"] == "requested" and not facts.tryboot
                    and facts.boot_id == trial["origin_boot_id"]
                    and 0 <= self.clock() - trial["requested_s"] < 30):
                return self._result(record)
            if not facts.tryboot or trial["state"] in {"staging", "rollback_requested", "manual_recovery_required"}:
                return self._rollback(state, boot, record, facts, "normal-baseline")
            if (facts.boot_id == trial["origin_boot_id"] or facts.configured_mhz != trial["mhz"]
                    or facts.platform_id != trial["platform_id"]
                    or not self._matches(boot, "config.txt", trial["baseline"].encode())
                    or not self._matches(boot, "tryboot.txt", trial["candidate"].encode())):
                return self._rollback(state, boot, record, facts, "boot-mismatch")
            try:
                self._health(trial["baseline_bits"])
            except (HelperError, OSError):
                return self._rollback(state, boot, record, facts, "health")
            if trial["state"] == "requested":
                now = self.clock()
                trial.update(state="active", active_boot_id=facts.boot_id,
                             started_s=now, deadline_s=now + CONFIRM_SECONDS)
                self._save(state, record)
            if trial.get("active_boot_id") != facts.boot_id:
                return self._rollback(state, boot, record, facts, "unexpected-boot")
            deadline = (trial["started_s"] + MAX_TRIAL_SECONDS if trial["state"] == "confirmed"
                        else trial["deadline_s"])
            if self.clock() >= deadline or self.clock() < trial["started_s"]:
                return self._rollback(state, boot, record, facts, "deadline")
            return self._result(record)

    def confirm_trial(self, trial_id, *, visible):
        with self._transaction() as (state, boot):
            facts, record = self._facts(), self._load(state)
            trial = record.get("trial", {})
            if (visible is not True or not isinstance(trial_id, str) or trial.get("id") != trial_id
                    or trial.get("state") != "active" or not facts.tryboot
                    or trial.get("active_boot_id") != facts.boot_id
                    or facts.configured_mhz != trial.get("mhz")
                    or facts.platform_id != trial.get("platform_id")
                    or boot.read(SAFE_MODE) is not None or self.clock() >= trial["deadline_s"]
                    or not self._normal_baseline_verified(boot, facts, trial)
                    or not self._matches(boot, "tryboot.txt", trial["candidate"].encode())):
                _fail("ConfirmationRejected", "Current healthy trial and timely visible confirmation are required")
            self._health(trial["baseline_bits"])
            trial["state"] = "confirmed"
            record["profile"] = {"mhz": trial["mhz"], "config_sha256": trial["config_sha256"],
                                 "platform_id": trial["platform_id"], "trial_id": trial["id"]}
            self._save(state, record)
            return self._result(record)

    def set_safe_mode(self):
        with self._transaction() as (state, boot):
            if boot.read(SAFE_MODE) is None:
                boot.write(SAFE_MODE, b"", expected=None)
            return self._rollback(state, boot, self._load(state), self._facts(), "safe-mode")

    def uninstall_ready(self):
        with self._transaction() as (state, _):
            record, facts = self._load(state), self._facts()
            self._health()
            return (not facts.tryboot and facts.configured_mhz == STOCK_MHZ
                    and record.get("trial", {}).get("state", "restored") == "restored"
                    and not record.get("trial", {}).get("cleanup_conflict", False)
                    and record.get("cpu", {}).get("state", "restored") == "restored")
