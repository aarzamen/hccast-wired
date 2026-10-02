# Advanced helper integration

IMPLEMENTED / UNIT-TESTED backend proposal. No installed D-Bus authority,
guardian service, live boot observer, readiness enrollment or hardware acceptance
is added by this module. Keep overclock trials **Unavailable** until those pieces
and the approved physical prerequisites are verified by the integrator.

## Callable boundary

`helper.advanced.AdvancedHelper(backend)` uses fixed production directories
`/var/lib/panelbridge` and `/boot/firmware`. The backend provides:

- `observe_boot() -> BootFacts`: root-trusted current model, boot ID, effective
  configured CPU MHz, actual tryboot flag, verified mounted boot identity/layout,
  secure-boot/boot-ramdisk/A-B state, and a platform fingerprint covering hardware,
  OS/kernel and boot firmware. Missing layout facts disable trials.
- `health() -> HealthSample`: trusted monotonic timestamp, temperature, raw
  throttling mask and data-error assessment. Unknown measurements must fail;
  do not substitute zero/false. Samples expire after ten seconds.
- `cpu_snapshot()`, `write_cpu(field, value)`, `reboot(trial=bool)`:
  `LinuxControls` implements these with fixed policy0 sysfs attributes and
  fixed reboot arguments. No shell or caller-specified command is supported.

Constructors' paths, owner and clock overrides are for trusted composition and
fixture tests. Never expose them, boot facts, health claims or readiness fields
as unprivileged API inputs. The service must authorize/enroll each caller using
its existing authority checks, serialize lifecycle integration, and notify the
user before a disruptive reboot. Backend callbacks must have bounded reads.

| Method | Behavior |
|---|---|
| `set_cpu(governor, cap_mhz)` | Offered governor plus a cap between current minimum and stock hardware maximum; durable first baseline and verified readback. |
| `restore_cpu()` | Restore recorded runtime values in the same boot; preserve conflicting outside edits. Runtime settings are not reapplied after reboot. |
| `start_trial(mhz)` | Verify readiness; journal intent; retain named normal baseline; create full alternate config; request one tryboot. |
| `guardian()` | Call at early service start and at least every five seconds. Restore incomplete transactions; enforce health, safe-mode flag and deadlines. |
| `confirm_trial(id, visible=True)` | Require active healthy trial and explicit physical/visible confirmation before the 120-second deadline. Save profile only. |
| `rollback()` | Preserve foreign candidate edits; request normal reboot only after separately verifying the saved normal baseline, supported layout and platform; require a subsequent normal stock boot. |
| `set_safe_mode()` | Create the app-defined boot flag and initiate restoration. The flag remains until explicitly removed from the boot volume. |
| `uninstall_ready()` | False while trial/runtime restoration remains outstanding; require observed stock boot and fresh health before removing the guardian. |

Returned trial state is `staging`, `requested`, `active`, `confirmed`,
`rollback_requested`, `manual_recovery_required` or `restored`. Errors are bounded `HelperError` codes;
keep journals private. A root-owned lock excludes overlapping operations.
`rollback_requested` stays pending until a new baseline boot is observed. Each
normal reboot request is attempted once per boot; failure requires attention,
not a tight retry loop. A requested tryboot has a 30-second reboot grace period.
The confirmation deadline starts when the guardian first sees the trial boot
and survives guardian restarts. Confirmed trials still end after four hours.
Activation, continuing supervision and confirmation require the original platform
fingerprint and supported boot layout. A fingerprint change cannot qualify a
trial or a saved profile.

## Trusted readiness record

The root-only `advanced-readiness.json` has `api_version: 1`, matching `boot_id`,
`platform_id` and SHA-256 `config_sha256`, plus literal `true` values for
`cooling_verified`, `supply_verified`, `recovery_verified`, `stock_load_verified`,
`backup_verified` and `semantics_reviewed`. Populate it only from retained actual
evidence. No public method creates this record. Rebind it only after verifying
the current normal boot. Physical confirmation is an observation, not another
request for build permission.

Trial prerequisites are an observed Pi 5 Model B, 2400 MHz normal configuration,
uncapped stock CPU policy, fresh temperature below 70 C, no current/history
throttle flags or data errors, and all readiness facts. New steps are at most
50 MHz above the last confirmed profile with matching config/platform hashes,
never above 2700 MHz. Missing confirmation, 70 C, new fault bits or lost health
requests a normal reboot when the normal baseline is still verifiable. An
unverifiable baseline requires immediate manual recovery, including at 75 C;
software cannot claim a successful thermal escape in that case. No voltage,
GPU/RAM, thermal-limit, EEPROM or OTP controls exist.

## Boot ownership and limits

`config.txt` is never written. `PANELBRIDGE_BASELINE.txt` retains its exact bytes;
`tryboot.txt` copies those bytes and appends only `[all]` and `arm_freq=N`.
File creation is journaled before mutation, uses fsync/atomic replacement and
rejects unsafe ownership, writable ancestors, symlinks and hardlinks. Existing
unrecorded boot assets are preserved, including an identical baseline file.
Restoration removes only the exact journaled candidate. An outside candidate
edit, symlink or other unsafe entry is retained with `cleanup_conflict: true`.
That conflict does not block a normal reboot if its separate baseline check
passes. The conflict continues to block uninstall after normal-boot proof until
it is resolved and the guardian verifies cleanup. Never overwrite foreign edits.

Before requesting normal reboot, recheck the exact saved normal configuration,
the original platform fingerprint and the supported boot layout, including after
the durable rollback record is written. Changed, unreadable or unsafe normal
config, changed platform or an unsupported layout persists
`manual_recovery_required: true`; `normal_reboot_required` remains false and no
automatic reboot is issued. The helper preserves those files and blocks
confirmation and uninstall. Restore the known baseline through the independent
recovery path before resetting; repeating reboot against a changed configuration
is not recovery. The guardian can resume after this manual repair is verified.

The limited parser rejects config includes, existing user tryboot/A-B layouts,
boot images, secure boot and existing voltage/clock/thermal/OTP directives.
`auto_initramfs=1` is ordinary Linux initramfs use and is preserved; this is
distinct from firmware `boot_ramdisk=1`. A changed baseline requires diagnosis
before another trial. The named baseline and journal remain as recovery evidence.

`PANELBRIDGE_SAFE_MODE` is an app flag checked by Linux service code. It has no
firmware meaning and cannot repair a hang before that service runs. A reset or
physical power cycle is still needed in early boot failures. If normal boot
files have changed, stop the workload and use independent recovery to inspect
and restore the saved baseline first. If that is unavailable, power down and
repair the boot medium on another computer, preserving unexpected edits before
restoring the baseline and creating the safe-mode flag. Do not blindly power
cycle into unverified normal configuration. Do not claim that deleting a trial file changes the
clock of a running trial, or remove the recovery service before normal-boot proof.

## Primary sources checked 2026-10-02

- [Raspberry Pi tryboot documentation](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html#fail-safe-os-updates-tryboot): one-shot alternate config; fixed `reboot '0 tryboot'`; secure boot and ramdisk exceptions.
- [Official autoboot source](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/autoboot.adoc): `tryboot_a_b=1` uses normal config; current tryboot status is in the bootloader device-tree node.
- [Official overclocking source](https://github.com/raspberrypi/documentation/blob/master/documentation/asciidoc/computers/config_txt/overclocking.adoc): `arm_freq` uses MHz, Pi 5 reference is 2400 MHz, and forbidden voltage/turbo controls carry additional risk. Keeping firmware voltage policy does not promise a fixed physical voltage or warranty coverage.
- [Linux CPUFreq documentation](https://www.kernel.org/doc/html/latest/admin-guide/pm/cpufreq.html): policy directories expose available governors and writable scaling limits in kHz; those settings do not establish measured energy savings.

The exact installed Pi 5 firmware behavior, current boot layout, cooling/supply,
stock-load measurements and physical recovery still require live verification.
No firmware update is part of this backend or an implied prerequisite action.
