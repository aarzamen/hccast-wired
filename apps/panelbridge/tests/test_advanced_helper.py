"""Advanced operations use private fixture files and a fake physical boundary."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helper.advanced import AdvancedHelper, BootFacts, HealthSample, LinuxControls
from helper.network import HelperError


class Backend:
    def __init__(self):
        self.facts = BootFacts("boot-a", "Raspberry Pi 5 Model B Rev 1.0", 2400,
                               False, True, False, False, False, "platform-a")
        self.sample = HealthSample(100.0, 50.0, 0, False)
        self.cpu = {"governor": "ondemand", "min_khz": 1500000, "max_khz": 2400000,
                    "hardware_max_khz": 2400000, "governors": ["ondemand", "performance", "powersave"]}
        self.reboots = []
        self.fail_governor = False
    def observe_boot(self):
        return self.facts
    def health(self):
        return self.sample
    def cpu_snapshot(self):
        return dict(self.cpu)
    def write_cpu(self, field, value):
        if field == "governor" and self.fail_governor:
            self.fail_governor = False
            raise OSError("injected write failure")
        self.cpu[field] = value
    def reboot(self, *, trial):
        self.reboots.append(trial)


@pytest.fixture
def rig(tmp_path):
    boot, state = tmp_path / "boot", tmp_path / "state"
    boot.mkdir(mode=0o700)
    state.mkdir(mode=0o700)
    (boot / "config.txt").write_text("# stock fixture\n[all]\narm_64bit=1\nauto_initramfs=1\n[cm4]\notg_mode=1\n")
    backend = Backend()
    clock = [100.0]
    helper = AdvancedHelper(backend, boot=boot.resolve(), state=state.resolve(),
                            trusted_uid=os.getuid(), clock=lambda: clock[0])
    readiness = {"api_version": 1, "boot_id": "boot-a", "platform_id": "platform-a",
                 "config_sha256": hashlib.sha256((boot / "config.txt").read_bytes()).hexdigest(),
                 "cooling_verified": True, "supply_verified": True, "recovery_verified": True,
                 "stock_load_verified": True, "backup_verified": True, "semantics_reviewed": True}
    (state / "advanced-readiness.json").write_text(json.dumps(readiness))
    return helper, backend, boot, state, clock


def enter_trial(rig):
    helper, backend, boot, state, clock = rig
    result = helper.start_trial(2450)
    backend.facts = replace(backend.facts, boot_id="boot-b", tryboot=True, configured_mhz=2450)
    return result, helper.guardian()


def test_cpu_apply_and_restore_saved_baseline(rig):
    helper, backend, *_ = rig
    helper.set_cpu("powersave", 1800)
    assert backend.cpu["max_khz"] == 1800000
    assert backend.cpu["governor"] == "powersave"
    helper.set_cpu("performance", 2000)
    assert helper.restore_cpu()["cpu_restored"] is True
    assert backend.cpu["max_khz"] == 2400000
    assert backend.cpu["governor"] == "ondemand"


@pytest.mark.parametrize("governor,cap", [("evil\ncommand", 1800), ("userspace", 1800),
                                           ("powersave", 1499), ("powersave", 2401),
                                           ("powersave", True), ("powersave", 1800.5)])
def test_cpu_rejects_unsupported_requests_without_mutation(rig, governor, cap):
    helper, backend, *_ = rig
    before = dict(backend.cpu)
    with pytest.raises(HelperError):
        helper.set_cpu(governor, cap)
    assert backend.cpu == before


def test_cpu_partial_failure_restores_and_external_edit_is_preserved(rig):
    helper, backend, *_ = rig
    backend.fail_governor = True
    with pytest.raises(HelperError):
        helper.set_cpu("powersave", 1800)
    assert backend.cpu["max_khz"] == 2400000
    helper.set_cpu("powersave", 1800)
    backend.cpu["max_khz"] = 1900000
    with pytest.raises(HelperError, match="Conflict"):
        helper.restore_cpu()
    assert backend.cpu["max_khz"] == 1900000


def test_trial_is_complete_alternate_config_with_exact_normal_baseline(rig):
    helper, backend, boot, state, _ = rig
    normal = (boot / "config.txt").read_bytes()
    result = helper.start_trial(2450)
    assert result["state"] == "requested"
    assert (boot / "config.txt").read_bytes() == normal
    assert (boot / "PANELBRIDGE_BASELINE.txt").read_bytes() == normal
    assert (boot / "tryboot.txt").read_bytes() == normal + b"\n# PanelBridge trial; normal config is unchanged\n[all]\narm_freq=2450\n"
    assert backend.reboots == [True]
    assert json.loads((state / "advanced.json").read_text())["trial"]["id"] == result["trial_id"]


@pytest.mark.parametrize("mhz", [2400, 2451, 2701, True, "2450", 2450.5])
def test_trial_ceiling_and_prior_step_are_enforced(rig, mhz):
    helper, backend, boot, *_ = rig
    with pytest.raises(HelperError):
        helper.start_trial(mhz)
    assert not (boot / "tryboot.txt").exists()
    assert backend.reboots == []


@pytest.mark.parametrize("field", ["cooling_verified", "supply_verified", "recovery_verified",
                                  "stock_load_verified", "backup_verified", "semantics_reviewed",
                                  "boot_id", "config_sha256", "platform_id"])
def test_trial_requires_every_trusted_readiness_binding(rig, field):
    helper, backend, boot, state, _ = rig
    path = state / "advanced-readiness.json"
    record = json.loads(path.read_text())
    record.pop(field)
    path.write_text(json.dumps(record))
    with pytest.raises(HelperError, match="Readiness"):
        helper.start_trial(2450)
    assert not (boot / "tryboot.txt").exists()
    assert backend.reboots == []


@pytest.mark.parametrize("field,value", [("model", "Raspberry Pi 4 Model B"),
    ("layout_verified", False), ("secure_boot", True), ("secure_boot", None),
    ("boot_ramdisk", True), ("ab_boot", True), ("configured_mhz", 2600), ("tryboot", True)])
def test_trial_requires_verified_stock_pi5_simple_boot_layout(rig, field, value):
    helper, backend, *_ = rig
    backend.facts = replace(backend.facts, **{field: value})
    with pytest.raises(HelperError):
        helper.start_trial(2450)


@pytest.mark.parametrize("name", ["tryboot.txt", "autoboot.txt", "boot.img", "tryboot.img", "boot.sig", "PANELBRIDGE_BASELINE.txt"])
def test_preexisting_boot_assets_are_preserved(rig, name):
    helper, backend, boot, *_ = rig
    (boot / name).write_text("user-owned bytes")
    with pytest.raises(HelperError):
        helper.start_trial(2450)
    assert (boot / name).read_text() == "user-owned bytes"
    assert backend.reboots == []


@pytest.mark.parametrize("directive", ["include other.txt", "force_turbo=1", "over_voltage_delta=100",
    "gpu_freq=960", "sdram_freq=4300", "temp_limit=90", "program_usb_boot_mode=1",
    "arm_freq=2500", "boot_ramdisk=1", "tryboot_a_b=1", "[tryboot]"])
def test_unsupported_config_is_rejected_even_in_conditional_sections(rig, directive):
    helper, backend, boot, state, _ = rig
    path = boot / "config.txt"
    path.write_text(path.read_text() + directive + "\n")
    readiness = json.loads((state / "advanced-readiness.json").read_text())
    readiness["config_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (state / "advanced-readiness.json").write_text(json.dumps(readiness))
    with pytest.raises(HelperError):
        helper.start_trial(2450)
    assert backend.reboots == []


@pytest.mark.parametrize("sample", [HealthSample(100, 70, 0, False), HealthSample(100, 75, 0, False),
    HealthSample(89, 50, 0, False), HealthSample(100, 50, 1, False),
    HealthSample(100, 50, 0, True), HealthSample(100, float("nan"), 0, False)])
def test_trial_rejects_unsafe_or_missing_health(rig, sample):
    helper, backend, *_ = rig
    backend.sample = sample
    with pytest.raises(HelperError):
        helper.start_trial(2450)


def test_confirm_needs_visible_confirmation_and_does_not_promote_normal_boot(rig):
    helper, backend, boot, state, _ = rig
    normal = (boot / "config.txt").read_bytes()
    trial, active = enter_trial(rig)
    assert active["state"] == "active"
    with pytest.raises(HelperError):
        helper.confirm_trial(trial["trial_id"], visible=False)
    result = helper.confirm_trial(trial["trial_id"], visible=True)
    assert result["state"] == "confirmed"
    assert (boot / "config.txt").read_bytes() == normal
    assert json.loads((state / "advanced.json").read_text())["profile"]["mhz"] == 2450
    assert helper.uninstall_ready() is False
    helper.rollback()
    assert backend.reboots == [True, False]
    backend.facts = replace(backend.facts, boot_id="boot-c", tryboot=False, configured_mhz=2400)
    assert helper.guardian()["state"] == "restored"
    assert helper.uninstall_ready() is True
    assert not (boot / "tryboot.txt").exists()


def test_guardian_restart_cannot_extend_confirmation_deadline(rig):
    helper, backend, boot, state, clock = rig
    enter_trial(rig)
    clock[0] = 219
    backend.sample = replace(backend.sample, monotonic_s=219)
    restarted = AdvancedHelper(backend, boot=boot.resolve(), state=state.resolve(),
                               trusted_uid=os.getuid(), clock=lambda: clock[0])
    assert restarted.guardian()["state"] == "active"
    clock[0] = 220
    backend.sample = replace(backend.sample, monotonic_s=220)
    assert restarted.guardian()["state"] == "rollback_requested"
    restarted.guardian()
    assert backend.reboots == [True, False]


def test_real_clock_progress_during_guardian_does_not_corrupt_deadline(rig):
    helper, backend, *_ = rig
    helper.start_trial(2450)
    backend.facts = replace(backend.facts, boot_id="boot-b", tryboot=True, configured_mhz=2450)
    ticks = iter([100 + tick / 1000 for tick in range(100)])
    helper.clock = lambda: next(ticks)
    assert helper.guardian()["state"] == "active"
    assert helper.guardian()["state"] == "active"


def test_guardian_does_not_remove_candidate_before_requested_reboot(rig):
    helper, backend, boot, _, clock = rig
    helper.start_trial(2450)
    assert helper.guardian()["state"] == "requested"
    assert (boot / "tryboot.txt").exists()
    clock[0] = 130
    backend.sample = replace(backend.sample, monotonic_s=130)
    assert helper.guardian()["state"] == "rollback_requested"
    assert not (boot / "tryboot.txt").exists()
    helper.guardian()
    assert backend.reboots == [True, False]


def test_unconfirmed_step_does_not_allow_larger_next_trial(rig):
    helper, backend, boot, state, _ = rig
    enter_trial(rig)
    helper.rollback()
    backend.facts = replace(backend.facts, boot_id="boot-c", tryboot=False, configured_mhz=2400)
    helper.guardian()
    readiness = json.loads((state / "advanced-readiness.json").read_text())
    readiness["boot_id"] = "boot-c"
    (state / "advanced-readiness.json").write_text(json.dumps(readiness))
    with pytest.raises(HelperError):
        helper.start_trial(2500)
    assert not (boot / "tryboot.txt").exists()


def test_confirmed_step_allows_only_the_next_bounded_step(rig):
    helper, backend, _, state, _ = rig
    trial, _ = enter_trial(rig)
    helper.confirm_trial(trial["trial_id"], visible=True)
    helper.rollback()
    backend.facts = replace(backend.facts, boot_id="boot-c", tryboot=False, configured_mhz=2400)
    helper.guardian()
    readiness = json.loads((state / "advanced-readiness.json").read_text())
    readiness["boot_id"] = "boot-c"
    (state / "advanced-readiness.json").write_text(json.dumps(readiness))
    with pytest.raises(HelperError):
        helper.start_trial(2501)
    assert helper.start_trial(2500)["mhz"] == 2500


def test_confirmed_trial_still_has_four_hour_limit(rig):
    helper, backend, _, _, clock = rig
    trial, _ = enter_trial(rig)
    helper.confirm_trial(trial["trial_id"], visible=True)
    clock[0] += 14400
    backend.sample = replace(backend.sample, monotonic_s=clock[0])
    assert helper.guardian()["state"] == "rollback_requested"
    assert backend.reboots == [True, False]


def test_safe_mode_without_trial_restores_runtime_policy_and_blocks_new_trial(rig):
    helper, backend, boot, *_ = rig
    helper.set_cpu("powersave", 1800)
    helper.set_safe_mode()
    assert (boot / "PANELBRIDGE_SAFE_MODE").is_file()
    assert backend.cpu["max_khz"] == 2400000
    assert backend.cpu["governor"] == "ondemand"
    with pytest.raises(HelperError, match="SafeMode"):
        helper.start_trial(2450)


def test_interrupted_staging_is_restored_without_reboot_or_normal_config_write(rig, monkeypatch):
    helper, backend, boot, _, _ = rig
    original = (boot / "config.txt").read_bytes()
    from helper import advanced
    write = advanced._Directory.write
    def fail_candidate(self, name, raw, *, expected):
        if name == "tryboot.txt":
            raise OSError("injected power loss")
        return write(self, name, raw, expected=expected)
    with monkeypatch.context() as patch:
        patch.setattr(advanced._Directory, "write", fail_candidate)
        with pytest.raises(HelperError):
            helper.start_trial(2450)
    assert helper.guardian()["state"] == "restored"
    assert (boot / "config.txt").read_bytes() == original
    assert not (boot / "tryboot.txt").exists()
    assert backend.reboots == []


def test_boot_directory_symlink_and_writable_parent_are_rejected(rig):
    helper, backend, boot, state, _ = rig
    link = boot.parent / "linked-boot"
    link.symlink_to(boot, target_is_directory=True)
    unsafe = AdvancedHelper(backend, boot=link, state=state.resolve(), trusted_uid=os.getuid())
    with pytest.raises(HelperError):
        unsafe.start_trial(2450)
    boot.chmod(0o777)
    with pytest.raises(HelperError):
        helper.start_trial(2450)
    assert backend.reboots == []


def test_hardlinked_normal_config_is_rejected(rig):
    helper, backend, boot, *_ = rig
    os.link(boot / "config.txt", boot / "second-link")
    with pytest.raises(HelperError):
        helper.start_trial(2450)
    assert backend.reboots == []


def test_same_content_unrecorded_baseline_is_not_claimed(rig):
    helper, _, boot, *_ = rig
    (boot / "PANELBRIDGE_BASELINE.txt").write_bytes((boot / "config.txt").read_bytes())
    with pytest.raises(HelperError, match="Conflict"):
        helper.start_trial(2450)


@pytest.mark.parametrize("record", [{"trial": []}, {"cpu": {"state": "applied"}},
                                   {"profile": {"mhz": 2700}}, {"trial": {"state": "restored"}}])
def test_corrupt_journal_fails_closed_with_bounded_error(rig, record):
    helper, backend, boot, state, _ = rig
    (state / "advanced.json").write_text(json.dumps({"api_version": 1, **record}))
    with pytest.raises(HelperError, match="InvalidState"):
        helper.guardian()
    assert backend.reboots == []
    assert not (boot / "tryboot.txt").exists()


def test_outside_journal_edit_during_cpu_apply_is_preserved(rig):
    helper, backend, _, state, _ = rig
    original_write = backend.write_cpu
    def write(field, value):
        original_write(field, value)
        if field == "governor":
            (state / "advanced.json").write_text('{"api_version": 1, "operator_note": "preserve"}\n')
    backend.write_cpu = write
    with pytest.raises(HelperError, match="Conflict"):
        helper.set_cpu("powersave", 1800)
    assert json.loads((state / "advanced.json").read_text())["operator_note"] == "preserve"


def test_config_changed_during_trial_staging_is_not_rebooted(rig, monkeypatch):
    helper, backend, boot, *_ = rig
    from helper import advanced
    write = advanced._Directory.write
    def change_config(self, name, raw, *, expected):
        write(self, name, raw, expected=expected)
        if name == "tryboot.txt":
            (boot / "config.txt").write_text("# outside edit\n")
    monkeypatch.setattr(advanced._Directory, "write", change_config)
    with pytest.raises(HelperError, match="Conflict"):
        helper.start_trial(2450)
    assert (boot / "config.txt").read_text() == "# outside edit\n"
    assert backend.reboots == []


def test_health_is_rechecked_after_durable_boot_file_writes(rig, monkeypatch):
    helper, backend, _, _, _ = rig
    from helper import advanced
    write = advanced._Directory.write
    def lose_health(self, name, raw, *, expected):
        write(self, name, raw, expected=expected)
        if name == "tryboot.txt":
            backend.sample = replace(backend.sample, temperature_c=75)
    monkeypatch.setattr(advanced._Directory, "write", lose_health)
    with pytest.raises(HelperError, match="Unhealthy"):
        helper.start_trial(2450)
    assert backend.reboots == []


def test_unknown_tryboot_prevents_uninstall_even_without_journal(rig):
    helper, backend, *_ = rig
    backend.facts = replace(backend.facts, tryboot=True)
    assert helper.uninstall_ready() is False
    with pytest.raises(HelperError, match="UnmanagedTrial"):
        helper.guardian()
    with pytest.raises(HelperError, match="UnmanagedTrial"):
        helper.set_safe_mode()
    assert backend.reboots == []


def test_fixed_reboot_commands_cannot_accept_arbitrary_arguments(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
    monkeypatch.setattr("helper.advanced.subprocess.run", run)
    backend = LinuxControls()
    backend.reboot(trial=True)
    backend.reboot(trial=False)
    assert [call[0] for call in calls] == [["/usr/sbin/reboot", "0 tryboot"], ["/usr/sbin/reboot"]]
    assert all(call[1]["timeout"] == 10 and "shell" not in call[1] for call in calls)
    with pytest.raises(HelperError):
        backend.reboot(trial="0 tryboot; bad")


def test_fixed_reboot_failure_is_bounded_and_not_retried(monkeypatch):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, 10)
    monkeypatch.setattr("helper.advanced.subprocess.run", run)
    with pytest.raises(HelperError, match="RebootFailed"):
        LinuxControls().reboot(trial=False)
    assert calls == [["/usr/sbin/reboot"]]


@pytest.mark.parametrize("change", ["temperature", "throttle", "lost_health", "data_errors", "safe_mode"])
def test_guardian_abandons_trial_on_health_failure_or_safe_mode(rig, change):
    helper, backend, boot, *_ = rig
    enter_trial(rig)
    if change == "temperature":
        backend.sample = replace(backend.sample, temperature_c=70)
    elif change == "throttle":
        backend.sample = replace(backend.sample, throttled_bits=1 << 16)
    elif change == "lost_health":
        backend.sample = replace(backend.sample, monotonic_s=0)
    elif change == "data_errors":
        backend.sample = replace(backend.sample, data_errors=True)
    else:
        (boot / "PANELBRIDGE_SAFE_MODE").write_bytes(b"")
    assert helper.guardian()["state"] == "rollback_requested"
    assert backend.reboots == [True, False]


@pytest.mark.parametrize("edit", ["changed", "symlink", "directory"])
def test_hot_trial_retreat_preserves_foreign_candidate_and_reboots_verified_baseline(rig, edit):
    helper, backend, boot, _, _ = rig
    enter_trial(rig)
    normal = (boot / "config.txt").read_bytes()
    candidate = boot / "tryboot.txt"
    if edit == "changed":
        candidate.write_text("# external candidate\narm_freq=2700\n")
    else:
        candidate.unlink()
        if edit == "symlink":
            candidate.symlink_to(boot / "config.txt")
        else:
            candidate.mkdir()
    backend.sample = replace(backend.sample, temperature_c=75)
    result = helper.guardian()
    assert result["state"] == "rollback_requested"
    assert result["cleanup_conflict"] is True
    assert result["manual_recovery_required"] is False
    assert backend.reboots == [True, False]
    assert (boot / "config.txt").read_bytes() == normal
    if edit == "changed":
        assert candidate.read_text() == "# external candidate\narm_freq=2700\n"
    elif edit == "symlink":
        assert candidate.is_symlink()
    else:
        assert candidate.is_dir()


@pytest.mark.parametrize("entrypoint", ["guardian", "rollback", "set_safe_mode"])
def test_changed_normal_baseline_never_receives_automatic_reboot(rig, entrypoint):
    helper, backend, boot, _, _ = rig
    enter_trial(rig)
    changed = b"[all]\narm_freq=2700\n"
    (boot / "config.txt").write_bytes(changed)
    backend.sample = replace(backend.sample, temperature_c=75)
    result = getattr(helper, entrypoint)()
    assert result["state"] == "manual_recovery_required"
    assert result["manual_recovery_required"] is True
    assert result["normal_reboot_required"] is False
    assert backend.reboots == [True]
    assert (boot / "config.txt").read_bytes() == changed
    helper.guardian()
    assert backend.reboots == [True]
    backend.sample = replace(backend.sample, temperature_c=50)
    assert helper.uninstall_ready() is False


@pytest.mark.parametrize("active", [False, True])
def test_guardian_rejects_platform_change_before_activation_or_during_trial(rig, active):
    helper, backend, _, state, _ = rig
    if active:
        enter_trial(rig)
    else:
        helper.start_trial(2450)
        backend.facts = replace(backend.facts, boot_id="boot-b", tryboot=True, configured_mhz=2450)
    backend.facts = replace(backend.facts, platform_id="platform-b")
    result = helper.guardian()
    assert result["state"] == "manual_recovery_required"
    assert backend.reboots == [True]
    assert "profile" not in json.loads((state / "advanced.json").read_text())


def test_confirmation_rejects_changed_platform_before_guardian_runs(rig):
    helper, backend, _, state, _ = rig
    trial, _ = enter_trial(rig)
    backend.facts = replace(backend.facts, platform_id="platform-b")
    with pytest.raises(HelperError, match="ConfirmationRejected"):
        helper.confirm_trial(trial["trial_id"], visible=True)
    assert "profile" not in json.loads((state / "advanced.json").read_text())


@pytest.mark.parametrize("entrypoint", ["guardian", "rollback", "set_safe_mode"])
@pytest.mark.parametrize("layout_change", ["config-symlink", "boot-image", "secure-boot", "layout-unknown"])
def test_recovery_requires_readable_normal_config_and_unchanged_supported_layout(rig, entrypoint, layout_change):
    helper, backend, boot, *_ = rig
    enter_trial(rig)
    normal = (boot / "config.txt").read_bytes()
    if layout_change == "config-symlink":
        (boot / "outside-config").write_bytes(normal)
        (boot / "config.txt").unlink()
        (boot / "config.txt").symlink_to(boot / "outside-config")
    elif layout_change == "boot-image":
        (boot / "boot.img").write_bytes(b"foreign boot image")
    elif layout_change == "secure-boot":
        backend.facts = replace(backend.facts, secure_boot=True)
    else:
        backend.facts = replace(backend.facts, layout_verified=False)
    result = getattr(helper, entrypoint)()
    assert result["manual_recovery_required"] is True
    assert backend.reboots == [True]
    assert (boot / "config.txt").read_bytes() == normal


def test_normal_baseline_is_rechecked_after_rollback_journal_write(rig, monkeypatch):
    helper, backend, boot, _, _ = rig
    enter_trial(rig)
    from helper import advanced
    write = advanced._Directory.write
    def change_during_save(self, name, raw, *, expected):
        write(self, name, raw, expected=expected)
        if name == "advanced.json" and json.loads(raw).get("trial", {}).get("state") == "rollback_requested":
            (boot / "config.txt").write_text("[all]\narm_freq=2700\n")
    monkeypatch.setattr(advanced._Directory, "write", change_during_save)
    assert helper.rollback()["state"] == "manual_recovery_required"
    assert backend.reboots == [True]


def test_manual_recovery_can_resume_only_after_saved_baseline_is_restored(rig):
    helper, backend, boot, _, _ = rig
    enter_trial(rig)
    baseline = (boot / "config.txt").read_bytes()
    (boot / "config.txt").write_text("[all]\narm_freq=2700\n")
    assert helper.guardian()["manual_recovery_required"] is True
    (boot / "config.txt").write_bytes(baseline)
    assert helper.guardian()["state"] == "rollback_requested"
    assert backend.reboots == [True, False]
    backend.facts = replace(backend.facts, boot_id="boot-c", tryboot=False, configured_mhz=2400)
    assert helper.guardian()["state"] == "restored"
    assert helper.uninstall_ready() is True


def test_baseline_restoration_remains_pending_until_conflicting_candidate_is_resolved(rig):
    helper, backend, boot, *_ = rig
    enter_trial(rig)
    (boot / "tryboot.txt").write_text("# retained outside edit\n")
    helper.rollback()
    backend.facts = replace(backend.facts, boot_id="boot-c", tryboot=False, configured_mhz=2400)
    result = helper.guardian()
    assert result["state"] == "restored"
    assert result["cleanup_conflict"] is True
    assert helper.uninstall_ready() is False
    assert (boot / "tryboot.txt").read_text() == "# retained outside edit\n"
    (boot / "tryboot.txt").unlink()
    assert helper.guardian()["cleanup_conflict"] is False
    assert helper.uninstall_ready() is True


def test_user_edit_of_trial_is_preserved_while_normal_reboot_is_requested(rig):
    helper, backend, boot, *_ = rig
    helper.start_trial(2450)
    (boot / "tryboot.txt").write_text("user edit")
    result = helper.rollback()
    assert result["cleanup_conflict"] is True
    assert result["state"] == "rollback_requested"
    assert (boot / "tryboot.txt").read_text() == "user edit"
    assert backend.reboots == [True, False]


@pytest.mark.parametrize("target", ["config.txt", "advanced-readiness.json", "advanced.json"])
def test_symlink_rejection_never_touches_target(rig, target):
    helper, backend, boot, state, _ = rig
    parent = boot if target == "config.txt" else state
    original = parent / target
    if original.exists():
        original.unlink()
    outside = parent / "outside"
    outside.write_text("do not modify")
    original.symlink_to(outside)
    with pytest.raises(HelperError):
        helper.start_trial(2450)
    assert outside.read_text() == "do not modify"
    assert backend.reboots == []


def test_linux_cpu_backend_writes_only_fixed_sysfs_fields(tmp_path):
    values = {"scaling_governor": "ondemand", "scaling_min_freq": "1500000",
              "scaling_max_freq": "2400000", "cpuinfo_max_freq": "2400000",
              "scaling_available_governors": "ondemand powersave performance"}
    for name, value in values.items():
        (tmp_path / name).write_text(value)
    controls = LinuxControls(cpu_path=tmp_path.resolve(), trusted_uid=os.getuid())
    assert controls.cpu_snapshot()["max_khz"] == 2400000
    controls.write_cpu("max_khz", 1800000)
    assert (tmp_path / "scaling_max_freq").read_text() == "1800000\n"
    with pytest.raises(HelperError):
        controls.write_cpu("../../outside", "bad")
