"""Trusted observer fixtures; no real Linux reads, control writes or reboots."""
import io as byte_io
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helper import advanced_observer as module
from helper.advanced_observer import LinuxAdvancedObserver, LinuxReadOnlyIO
from helper.network import Budget, HelperError

DT = "/sys/firmware/devicetree/base"
BOOT_ID = b"11111111-1111-4111-8111-111111111111\n"


class FixtureIO:
    def __init__(self):
        self.files = {
            DT + "/model": b"Raspberry Pi 5 Model B Rev 1.0\0",
            DT + "/compatible": b"raspberrypi,5-model-b\0brcm,bcm2712\0",
            DT + "/serial-number": b"0000000012345678\0",
            DT + "/system/linux,revision": bytes.fromhex("00d04170"),
            DT + "/chosen/bootloader/tryboot": b"\0\0\0\0",
            DT + "/chosen/bootloader/signed": b"\0\0\0\0",
            DT + "/chosen/bootloader/partition": b"\0\0\0\1",
            DT + "/chosen/bootloader/boot-mode": b"\0\0\0\4",
            DT + "/chosen/bootloader/version": b"0123456789abcdef0123456789abcdef01234567\0",
            DT + "/chosen/bootloader/build_timestamp": bytes.fromhex("69000000"),
            "/proc/sys/kernel/random/boot_id": BOOT_ID,
            "/proc/sys/kernel/osrelease": b"6.18.0+rpt-rpi-2712\n",
            "/proc/version": b"Linux version 6.18.0+rpt-rpi-2712 (builder) #1 SMP\n",
            "/usr/lib/os-release": b'ID=debian\nVERSION_ID="13"\n',
            "/proc/self/mountinfo": (
                b"31 1 8:2 / / rw,relatime - ext4 /dev/sda2 rw\n"
                b"32 31 8:1 / /boot/firmware rw,relatime - vfat /dev/sda1 rw,fmask=0022\n"),
            "/sys/dev/block/8:1/partition": b"1\n",
            "/sys/dev/block/8:2/partition": b"2\n",
            "/boot/firmware/config.txt": b"[all]\narm_64bit=1\nauto_initramfs=1\n",
            "/sys/class/thermal/thermal_zone0/temp": b"45000\n",
            "/sys/class/thermal/thermal_zone0/type": b"cpu-thermal\n",
        }
        self.links = {
            "/sys/dev/block/8:1": "/sys/devices/platform/usb1/1-1/block/sda/sda1",
            "/sys/dev/block/8:2": "/sys/devices/platform/usb1/1-1/block/sda/sda2",
        }
        self.commands = {
            ("/usr/bin/vcgencmd", "get_config", "int"): b"arm_freq=2400\narm_boost=1\n",
            ("/usr/bin/vcgencmd", "get_config", "boot_ramdisk"): b"boot_ramdisk=0\n",
            ("/usr/bin/vcgencmd", "get_config", "tryboot_a_b"): b"tryboot_a_b=0\n",
            ("/usr/bin/vcgencmd", "get_throttled"): b"throttled=0x0\n",
        }
        self.records = [b"6,0,0,-;Booting Linux on physical CPU\n",
                        b"6,1,10,-;Linux version 6.18.0+rpt-rpi-2712 (builder) #1 SMP\n",
                        b"6,2,1000000,-;Mounted root filesystem\n"]
        self.reads, self.executed = [], []

    def read(self, path, limit):
        self.reads.append(path)
        value = self.files[path] if path in self.files else None
        if value is None:
            raise FileNotFoundError(path)
        if isinstance(value, Exception):
            raise value
        if len(value) > limit:
            raise HelperError("ObservationUnavailable", "Fixture read exceeds bound")
        return value

    def exists(self, path):
        return path in self.files

    def link(self, path):
        return self.links[path]

    def command(self, argv, timeout, limit):
        assert 0 < timeout <= 2
        self.executed.append(tuple(argv))
        value = self.commands[tuple(argv)]
        if isinstance(value, Exception):
            raise value
        assert len(value) <= limit
        return value

    def kernel_records(self, budget):
        budget.check()
        return list(self.records)


@pytest.fixture
def rig():
    io = FixtureIO()
    return LinuxAdvancedObserver(io=io, clock=lambda: 100.0), io


def test_observes_supported_stock_boot_and_only_hashes_private_identity(rig):
    observer, io = rig
    facts = observer.observe_boot()
    assert facts.configured_mhz == 2400 and facts.tryboot is False
    assert facts.layout_verified is True
    assert facts.secure_boot is facts.boot_ramdisk is facts.ab_boot is False
    assert len(facts.platform_id) == 64
    assert "12345678" not in repr(facts)
    assert not any("eeprom" in path or "otp" in path for path in io.reads)


def test_trial_clock_and_boot_id_change_do_not_change_platform_identity(rig):
    observer, io = rig
    baseline = observer.observe_boot()
    io.files["/proc/sys/kernel/random/boot_id"] = b"22222222-2222-4222-8222-222222222222\n"
    io.files[DT + "/chosen/bootloader/tryboot"] = b"\0\0\0\1"
    io.commands[("/usr/bin/vcgencmd", "get_config", "int")] = b"arm_freq=2450\n"
    trial = observer.observe_boot()
    assert trial.tryboot is True and trial.configured_mhz == 2450
    assert trial.platform_id == baseline.platform_id


@pytest.mark.parametrize("path,value", [
    (DT + "/model", b"Raspberry Pi 500 Rev 1.0\0"),
    (DT + "/compatible", b"raspberrypi,5-model-b\0brcm,bcm2711\0"),
    (DT + "/chosen/bootloader/tryboot", b"0\n"),
    (DT + "/chosen/bootloader/tryboot", b"\1\0\0\0"),
    (DT + "/chosen/bootloader/tryboot", FileNotFoundError()),
    ("/proc/sys/kernel/random/boot_id", b"not-a-boot-id"),
])
def test_core_identity_or_tryboot_ambiguity_fails_without_inventing_stock_boot(rig, path, value):
    observer, io = rig
    io.files[path] = value
    with pytest.raises(HelperError):
        observer.observe_boot()


@pytest.mark.parametrize("raw", [b"", b"arm_freq=2400\narm_freq=2450\n", b"arm_freq=oops\n", b"arm_freq=0\n"])
def test_effective_frequency_requires_unambiguous_firmware_output(rig, raw):
    observer, io = rig
    io.commands[("/usr/bin/vcgencmd", "get_config", "int")] = raw
    with pytest.raises(HelperError):
        observer.observe_boot()


@pytest.mark.parametrize("path", [DT + "/chosen/bootloader/signed", DT + "/serial-number",
                                  DT + "/chosen/bootloader/version", "/proc/self/mountinfo"])
def test_missing_layout_evidence_preserves_cpu_facts_but_disables_trials(rig, path):
    observer, io = rig
    io.files.pop(path)
    facts = observer.observe_boot()
    assert facts.configured_mhz == 2400 and facts.tryboot is False
    assert facts.layout_verified is False
    if path.endswith("signed"):
        assert facts.secure_boot is None


@pytest.mark.parametrize("key", ["boot_ramdisk", "tryboot_a_b"])
def test_unsupported_boot_flag_query_stays_unknown(rig, key):
    observer, io = rig
    io.commands[("/usr/bin/vcgencmd", "get_config", key)] = b"error=1\n"
    facts = observer.observe_boot()
    assert facts.layout_verified is False
    assert getattr(facts, "ab_boot" if key == "tryboot_a_b" else key) is None


@pytest.mark.parametrize("name", ["autoboot.txt", "boot.img", "tryboot.img", "boot.sig", "tryboot.sig"])
def test_alternate_boot_assets_are_not_certified(rig, name):
    observer, io = rig
    io.files["/boot/firmware/" + name] = b"unrelated asset"
    assert observer.observe_boot().layout_verified is False


def test_health_requires_complete_kernel_sequence_and_reads_no_private_journal(rig):
    observer, io = rig
    health = observer.health()
    assert health.monotonic_s == 100 and health.temperature_c == 45
    assert health.throttled_bits == 0 and health.data_errors is False
    assert all("journal" not in path for path in io.reads)


@pytest.mark.parametrize("records", [[], [b"6,9,0,-;Linux version fixture\n"],
    [b"6,0,0,-;Linux version fixture\n", b"6,2,10,-;gap\n"],
    [b"broken record"], [b"14,0,0,-;Linux version fake userspace marker\n"]])
def test_incomplete_or_untrusted_kernel_coverage_is_unknown_failure(rig, records):
    observer, io = rig
    io.records = records
    with pytest.raises(HelperError, match="HealthUnavailable"):
        observer.health()


@pytest.mark.parametrize("record", [b"3,3,2000000,-;hardware failure\n",
                                     b"4,3,2000000,-;Buffer I/O error on dev sda\n"])
def test_kernel_faults_are_reported_without_exposing_log_content(rig, record):
    observer, io = rig
    io.records.append(record)
    assert observer.health().data_errors is True


def test_userspace_injected_error_text_is_not_a_kernel_fault(rig):
    observer, io = rig
    io.records.append(b"11,3,2000000,-;Kernel panic: untrusted userspace text\n")
    assert observer.health().data_errors is False


@pytest.mark.parametrize("path,value", [
    ("/sys/dev/block/8:1/partition", b"2\n"),
    (DT + "/chosen/bootloader/partition", b"\0\0\0\2"),
    (DT + "/chosen/bootloader/boot-mode", b"\0\0\0\2"),
    (DT + "/chosen/bootloader/signed", b"\0\0\0\1"),
    (DT + "/chosen/bootloader/signed", b"zero"),
    ("/boot/firmware/config.txt", b"include other.txt\n"),
    ("/boot/firmware/config.txt", b"[tryboot]\narm_freq=2450\n"),
])
def test_unsupported_or_ambiguous_layout_keeps_core_facts_without_qualification(rig, path, value):
    observer, io = rig
    io.files[path] = value
    facts = observer.observe_boot()
    assert facts.configured_mhz == 2400 and facts.layout_verified is False


@pytest.mark.parametrize("change", ["duplicate", "bind", "readonly", "wrong_fs", "different_disk"])
def test_mount_identity_and_supported_layout_must_match(rig, change):
    observer, io = rig
    raw = io.files["/proc/self/mountinfo"]
    if change == "duplicate":
        raw += raw.splitlines()[1] + b"\n"
    elif change == "bind":
        raw = raw.replace(b"8:1 / /boot", b"8:1 /subdir /boot")
    elif change == "readonly":
        raw = raw.replace(b"/boot/firmware rw,", b"/boot/firmware ro,")
    elif change == "wrong_fs":
        raw = raw.replace(b"- vfat", b"- ext4")
    else:
        io.links["/sys/dev/block/8:2"] = "/sys/devices/platform/usb1/1-2/block/sdb/sdb2"
    io.files["/proc/self/mountinfo"] = raw
    assert observer.observe_boot().layout_verified is False


@pytest.mark.parametrize("path", [DT + "/serial-number", DT + "/chosen/bootloader/version",
                                  "/proc/sys/kernel/osrelease", "/usr/lib/os-release", "/boot/firmware/config.txt"])
def test_platform_fingerprint_changes_with_bound_hardware_software_or_configuration(rig, path):
    observer, io = rig
    previous = observer.observe_boot().platform_id
    if path.endswith("serial-number"):
        io.files[path] = b"0000000087654321\0"
    elif path.endswith("/version"):
        io.files[path] = b"abcdef0123456789abcdef0123456789abcdef0123\0"
    else:
        io.files[path] += b"\n# changed\n"
    assert observer.observe_boot().platform_id != previous


@pytest.mark.parametrize("record", [b"6,3,2000000,c;partial log fragment\n",
    b"4,3,2000000,-;printk: 12 messages suppressed\n"])
def test_fragmented_or_suppressed_kernel_evidence_is_not_certified_complete(rig, record):
    observer, io = rig
    io.records.append(record)
    with pytest.raises(HelperError, match="HealthUnavailable"):
        observer.health()


@pytest.mark.parametrize("temperature,throttled", [(b"NaN\n", b"throttled=0x0\n"),
    (b"45000\n", b"throttled=0x10\n"), (b"45000\n", b"unavailable\n")])
def test_malformed_health_is_unknown_failure(rig, temperature, throttled):
    observer, io = rig
    io.files["/sys/class/thermal/thermal_zone0/temp"] = temperature
    io.commands[("/usr/bin/vcgencmd", "get_throttled")] = throttled
    with pytest.raises(HelperError, match="HealthUnavailable"):
        observer.health()


def test_boot_change_during_observation_invalidates_the_result(rig):
    observer, io = rig
    original = io.read
    calls = []
    def read(path, limit):
        if path.endswith("boot_id"):
            calls.append(path)
            if len(calls) == 2:
                return b"22222222-2222-4222-8222-222222222222\n"
        return original(path, limit)
    io.read = read
    with pytest.raises(HelperError, match="BootObservationUnavailable"):
        observer.observe_boot()


def test_observation_does_not_accept_read_or_command_results_after_its_deadline(rig):
    observer, io = rig
    clock = [100.0]
    observer.clock = lambda: clock[0]
    original = io.command
    def command(argv, timeout, limit):
        value = original(argv, timeout, limit)
        clock[0] += 9
        return value
    io.command = command
    with pytest.raises(HelperError, match="BootObservationUnavailable"):
        observer.observe_boot()


@pytest.mark.parametrize("value", [b"gpu-thermal\n", b"", FileNotFoundError()])
def test_temperature_requires_the_expected_cpu_sensor(rig, value):
    observer, io = rig
    io.files["/sys/class/thermal/thermal_zone0/type"] = value
    with pytest.raises(HelperError, match="HealthUnavailable"):
        observer.health()


@pytest.mark.parametrize("operation", [
    lambda io: io.read("/etc/shadow", 32),
    lambda io: io.exists("/boot/firmware/unlisted"),
    lambda io: io.link("/sys/dev/block/8:1/../../other"),
    lambda io: io.command(("/usr/bin/vcgencmd", "otp_dump"), 1, 64),
    lambda io: io.command(("/usr/bin/vcgencmd", "get_throttled"), 3, 64),
])
def test_production_io_rejects_outside_authority_before_access(monkeypatch, operation):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    def forbidden(*args, **kwargs):
        pytest.fail("Out-of-contract access reached the operating system")
    monkeypatch.setattr(module.os, "open", forbidden)
    monkeypatch.setattr(module.os, "lstat", forbidden)
    monkeypatch.setattr(module.subprocess, "Popen", forbidden)
    with pytest.raises(HelperError):
        operation(LinuxReadOnlyIO())


def test_production_io_requires_root_before_open(monkeypatch):
    monkeypatch.setattr(module.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(module.os, "open", lambda *a, **k: pytest.fail("Unprivileged read"))
    with pytest.raises(HelperError):
        LinuxReadOnlyIO().read(module.BOOT_ID, 64)


@pytest.mark.parametrize("ending", ["caught_up", "overrun", "record_limit"])
def test_production_kernel_reader_is_readonly_bounded_and_closes(monkeypatch, ending):
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    opened, closed, reads = [], [], []
    def open_fd(path, flags):
        opened.append((path, flags))
        return 99
    def read_fd(fd, size):
        reads.append((fd, size))
        if len(reads) == 2 and ending == "caught_up":
            raise BlockingIOError()
        if len(reads) == 2 and ending == "overrun":
            raise OSError("ring overrun")
        return b"6,0,0,-;Linux version fixture\n"
    monkeypatch.setattr(module.os, "open", open_fd)
    monkeypatch.setattr(module.os, "read", read_fd)
    monkeypatch.setattr(module.os, "fstat", lambda fd: SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_uid=0))
    monkeypatch.setattr(module.os, "close", closed.append)
    budget = Budget(101, clock=lambda: 100)
    if ending == "caught_up":
        assert len(LinuxReadOnlyIO().kernel_records(budget)) == 1
    else:
        with pytest.raises((OSError, HelperError)):
            LinuxReadOnlyIO().kernel_records(budget)
    assert opened == [("/dev/kmsg", os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)]
    assert closed == [99] and len(reads) <= 16384
    assert all(size == 16384 for _, size in reads)


@pytest.mark.parametrize("case", ["valid", "owner", "writable", "directory", "oversized"])
def test_production_file_reads_reject_unsafe_metadata_and_bound_bytes(monkeypatch, case):
    class Stream(byte_io.BytesIO):
        def fileno(self):
            return 99
    stream = Stream(b"12345" if case == "oversized" else b"1234")
    flags = []
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.os, "open", lambda path, value: flags.append((path, value)) or 99)
    monkeypatch.setattr(module.os, "fdopen", lambda fd, mode: stream)
    mode = (stat.S_IFDIR if case == "directory" else stat.S_IFREG) | (0o666 if case == "writable" else 0o444)
    monkeypatch.setattr(module.os, "fstat", lambda fd: SimpleNamespace(st_mode=mode, st_uid=1000 if case == "owner" else 0))
    if case == "valid":
        assert LinuxReadOnlyIO().read(module.BOOT_ID, 4) == b"1234"
    else:
        with pytest.raises(HelperError):
            LinuxReadOnlyIO().read(module.BOOT_ID, 4)
    assert flags == [(module.BOOT_ID, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)]
    assert stream.closed


@pytest.mark.parametrize("case", ["valid", "oversized", "deadline", "nonzero"])
def test_production_commands_have_fixed_argv_finite_wait_output_cap_and_cleanup(monkeypatch, case):
    class Output:
        closed = False
        def fileno(self):
            return 99
        def close(self):
            self.closed = True
    class Child:
        stdout = Output()
        killed = False
        done = False
        waits = []
        def poll(self):
            return 0 if self.done else None
        def kill(self):
            self.killed = True
        def wait(self, timeout):
            self.waits.append(timeout)
            self.done = True
            return 1 if case == "nonzero" else 0
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, source, event):
            assert source is child.stdout
        def select(self, timeout):
            assert 0 < timeout <= 1
            return [] if case == "deadline" else [True]
    child, called = Child(), []
    chunks = iter([b"12345" if case == "oversized" else b"1234", b""])
    monkeypatch.setattr(module.os, "geteuid", lambda: 0)
    monkeypatch.setattr(module.time, "monotonic", lambda: 100)
    monkeypatch.setattr(module.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(module.os, "read", lambda fd, limit: next(chunks))
    monkeypatch.setattr(module.subprocess, "Popen", lambda argv, **kw: called.append((argv, kw)) or child)
    argv = ("/usr/bin/vcgencmd", "get_throttled")
    if case == "valid":
        assert LinuxReadOnlyIO().command(argv, 1, 4) == b"1234"
    else:
        with pytest.raises(HelperError):
            LinuxReadOnlyIO().command(argv, 1, 4)
    assert called[0][0] == argv and "shell" not in called[0][1]
    assert called[0][1]["env"] == {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
    assert child.stdout.closed and child.done
    assert child.killed is (case in {"oversized", "deadline"})
    assert all(0 < timeout <= 1 for timeout in child.waits)
