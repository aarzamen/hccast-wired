# AGENTS.md — PanelBridge and HCCAST

This repository contains experimental owned-hardware display software. It is a
hobbyist interoperability project and is not clinical software. Publishing source
does not mean the standalone display product has passed its acceptance gates.

## 1. Authority and workflow

Read this contract, [MODEL_CONTEXT.md](MODEL_CONTEXT.md), the relevant source,
tests and evidence summary before changing behavior. A human's current assignment
defines the authorized scope. Hardware access, package installation, SSH, sudo,
reboots and network changes require an explicit task manifest identifying the
host, device, operations and restoration path. Once approved, that manifest covers
ordinary retries, fixes and tests throughout the assigned work; do not repeatedly
request the same permission. Private development approvals and host bindings are
not included in this public checkout and cannot be inferred from its existence.

Use isolated uv environments with Python 3.12 or 3.13 for development. Preserve
the known-good legacy runtime. Run meaningful tests for changed behavior and
report the actual commands, outcomes and missing evidence. Do not add simulated
results to a physical acceptance record.

The intended PanelBridge outcome is a Raspberry Pi main screen from startup
through daily desktop use and shutdown without a second monitor. Prioritize
installation, startup, reconnect, recovery and restoration acceptance. Measure
useful frame rate, frame loss, latency, CPU load, temperature and power before
selecting performance changes. Source rate, encoded rate and receiver refresh are
different measurements. Optional OLED rollout is deferred.

## 2. Evidence and platform boundaries

### Claim labels

- **OBSERVED** — directly inspected source, descriptor, command output or user report.
- **INFERRED** — reasoned conclusion with its assumptions identified.
- **IMPLEMENTED** — code exists; execution and correctness are not implied.
- **UNIT-TESTED** — named deterministic checks passed for the specified source.
- **HARDWARE-VERIFIED** — ran on the named physical host/device pairing; visible output requires panel observation or physical capture.
- **REPRODUCED** — independently repeated on another host or unit; state what changed.

Apply labels to individual claims. Qshot host USB, RK-X40F gadget USB, EBPSI
Miracast and TTQ DisplayPort are separate paths. Results do not transfer between
them. EBPSI USB video is unavailable; active USB investigation is deferred. The
current PanelBridge approach uses the normal labwc desktop and Wi-Fi Direct.
VNC/noVNC is excluded from its display path. Physical startup and recovery gates
remain open even when bounded playback and software checks pass.

## 3. Hardware ownership and recovery

One integrator owns all hardware, SSH, network discovery and system configuration.
At most two workers run concurrently with distinct file ownership and no recursive
delegation. Shared schemas and integration belong to the integrator.

Before authorized physical work, verify host/device identity, current connections,
saved baseline and independent recovery. Keep Ethernet for recovery when Wi-Fi is
used for the panel. Use finite deadlines, preserve failed runs and restore only
recorded app changes. Three stagnant attempts require diagnosis. Never operate
unrelated buses or storage, detach unreviewed drivers or guess vendor commands.

Overclock trials require supported setting semantics, adequate supply/cooling,
stock measurements and tested rollback. Frequency-only Pi 5 trials are capped at
2.7 GHz in steps no larger than 50 MHz. Back off at 70 degrees C; abort at 75,
new undervoltage/throttling, data errors or lost health signal. No voltage override,
force_turbo, thermal-limit increase or OTP write is allowed. Tryboot does not prove
recovery from every early boot failure.

## 4. Always request fresh approval

- Writing device firmware/flash, entering update modes or cross-flashing.
- Bootloader EEPROM changes and irreversible OTP/warranty-bit operations.
- Publishing source/artifacts/releases, changing visibility, force-pushing or rewriting Git history beyond the current explicit publication assignment.
- Reading/exporting/modifying credentials or adding accounts, keys or access grants; approved existing authentication may be used without exposing secrets.
- Deleting unrelated files, destructive formatting/reimaging or work outside the approved host/device/operation manifest.

## 5. Privacy, licenses and completion

Keep raw logs, media, host bindings, serial numbers, MAC addresses, credentials,
personal paths and internal planning private. Use an explicit publication file
allowlist and review it; ignore rules alone are insufficient. Preserve existing
MIT notices. PanelBridge has a separate GPL-3.0-or-later license and retains its
upstream notices and matching source. Do not invent contributor/model attribution.

Completion requires evidence for fresh installation, desktop output without HDMI,
reboot/reconnect/power cycle, persistent setting changes, bad-setting recovery and
clean uninstall. A missing gate stays missing. Lead status with the actual state;
software snapshots and source publication do not complete the product.
