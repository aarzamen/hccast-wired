# Progressive test plan

The Jetson protocol path has already reached visible wired video. The next target
is Raspberry Pi parity. This plan separates software verification from physical
checkpoints and does not grant authority to run hardware.

## Phase 0 — software-only gate

With the isolated development environment already provisioned:

```bash
uv run --no-sync pytest -p no:cacheprovider -o addopts= -q
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
python3 -m compileall -q src tests
```

Passing this phase supports `UNIT-TESTED` claims only.

## Phase 1 — choose one Raspberry Pi target

Prefer a Pi 4 or Pi 5 configuration that can be powered independently while the
device-capable USB port is reserved for data. Record:

```text
exact board revision
operating-system image and release
kernel and device tree
power source and voltage/current rating
USB port and cable topology
UDC entries
ConfigFS and FunctionFS support
current gadget ownership
expected stopped state
```

Do not infer device capability from the connector shape or an OTG label.

## Phase 2 — passive platform preflight

This phase is read-only. It must establish:

- at least one usable current UDC;
- no stale HCCAST gadget, mount, process, or display socket;
- current ConfigFS gadget ownership;
- the correct platform-specific state to restore after a run;
- availability of the exact reviewed source and interpreter;
- availability of a known H.264 fixture.

If the UDC is absent or permanently host-oriented, stop. That is a platform/image
finding, not a HCCAST protocol failure.

## Phase 3 — stopped-state cleanup design

Before an active attempt, define and software-test the Pi cleanup contract:

```text
remove only attempt-owned processes
unmount only attempt-owned FunctionFS mount
unbind and remove only attempt-owned ConfigFS gadget
restore any pre-existing Pi gadget if one was present
verify final UDC ownership and process/mount absence
```

Jetson's `nv-l4t-usb-device-mode.service` is not portable. A Pi implementation
must use the selected image's actual state.

## Phase 4 — direct USB personality

After a human authorizes one exact physical checkpoint:

```text
Linux gadget VID:PID 18d1:2d00
one vendor interface
bulk OUT and bulk IN
FunctionFS enable
```

Success is endpoint configuration. No video belongs in this phase.

## Phase 5 — HCCAST identity

Send `SETR` through the existing session implementation and require a valid parsed
`SETV`. Preserve the screen product/version returned by the run.

Success is a valid `SETV`, not merely a successful bulk write.

## Phase 6 — known-good video fixture

Use the verified portrait metadata and known-good Annex-B H.264 fixture. Vary no
other layer until physical pixels appear.

Record separately:

- first still frame;
- continuous moving output;
- elapsed time and bytes/access units;
- any visual corruption;
- physical screen state before and after cleanup.

## Phase 7 — virtual surface

After fixture parity:

```text
640x1136 Xvfb surface
10 fps initial target
baseline Annex-B H.264
access-unit alignment
visible pointer
```

First use a local deterministic page. Browser networking and online video are
later supervised tests, not protocol prerequisites.

## Phase 8 — supervised reliability

Increase duration in bounded stages:

1. 10 minutes of deterministic motion.
2. 20 minutes of local kiosk interaction.
3. One hour of representative content.
4. Disconnect/reconnect.
5. Screen power cycle.
6. Source reboot with screen attached.

Each stage preserves telemetry and verifies cleanup. Unattended overnight or
multi-hour operation follows only after the bounded stages are stable.

## Historical macOS branch

Completed macOS diagnostics established USB roles and a transient direct-host
interface but no valid HCCAST `SETV` and no physical wired video. Those tests are
preserved in [VALIDATION.md](VALIDATION.md) and are not the current path to
Raspberry Pi support.

## Deferred compatibility

After Raspberry Pi parity:

- second monitor unit/revision;
- R36S with a gadget-capable kernel/device tree;
- audio;
- lower-power encoder optimization;
- broader recovery behavior.

No platform becomes `REPRODUCED` without an independently repeated physical result.
