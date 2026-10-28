# Roadmap

**Current scope, 2026-10-27:** Pi 500 extended desktop on LUODIER/EBPSI with
HDMI retained. Physical video output is demonstrated; repeatable operation is
unfinished. See [scope](docs/SCOPE.md) and [Pi 500 results](docs/PI500.md).

## Next Pi 500 steps

1. Diagnose the observed freeze and teardown timeout without losing Ethernet recovery.
2. Qualify repeated manual start/stop/reconnect and clean network restoration.
3. Verify reboot/power-cycle recovery, sustained playback and clean upgrade/uninstall.
4. Build and inspect distributable binaries with matching source and notices;
   publish a tagged release only after its actual acceptance gates pass.

Qshot's Pi 500 wired motion test and TTQ's inconclusive connection check are
separate compatibility results. Standalone Pi 5 boot, advanced controls, optional
OLED rollout, RK-X40F gadget reproduction and firmware work remain separate or
parked tracks. Historical results follow.

## Current technical alpha

- `HARDWARE-VERIFIED`: one Jetson Orin Nano configuration and one
  RK-X40F-family unit have a bounded direct wired H.264 path.
- `IMPLEMENTED` and `UNIT-TESTED`: the live virtual-display controller and its
  checked cleanup/evidence components exist in source.
- `OBSERVED`: isolated Chromium/Xvfb desktop interaction and supervised video
  playback were visible on the physical panel.
- `REPRODUCED`: Qshot five-second host-USB fixture on Jetson and Pi 5, with
  correct motion/proportions.
- `HARDWARE-VERIFIED`: one-minute Pi desktop/video and five-minute Mac-controlled
  desktop playback at 1280x720 and a 30 fps target. Longer sessions and automatic
  reconnect remain future checkpoints.

## Parked gadget portability target: Raspberry Pi reproduction

Reproduce the Jetson reference path without redesigning the protocol:

1. Identify a Pi model, power topology, kernel, and port that expose a real UDC.
2. Verify direct `18d1:2d00` enumeration and FunctionFS bulk configuration.
3. Receive and parse a valid `SETV` after `SETR`.
4. Send the known-good portrait `SINF` and H.264 fixture.
5. Observe visible pixels and verify complete platform cleanup.
6. Only then adapt the isolated virtual-desktop pipeline and measure stability.

See [docs/REPRODUCTION.md](docs/REPRODUCTION.md) and
[docs/TESTED_HARDWARE.md](docs/TESTED_HARDWARE.md).

## Parked Qshot V2 diagnostic track

- `OBSERVED`, recorded in the September 27 session: powered non-Pro enumeration
  was stable, but macOS denied interface access before an HCCAST request.
- `OBSERVED`, recorded in the same session: the official Pro firmware contains
  matching USB descriptor templates and HCCast components.
- `INFERRED`: a shared platform is plausible. A matching non-Pro firmware
  package and firmware interchangeability remain unverified. Non-Pro protocol
  response and visible host-USB video have since been verified on Jetson and Pi 5.
- Original temporary evidence was missing at closeout. The retained findings
  and evidence gap are recorded in [docs/VALIDATION.md](docs/VALIDATION.md).

## After Raspberry Pi parity

- Test disconnect/reconnect, monitor power cycles, reboot recovery, and longer
  supervised stability runs.
- Evaluate lower-power encoding paths suitable for a small Pi deployment.
- Add audio only after the video path is stable.
- Evaluate the R36S and additional monitor revisions as separate compatibility
  targets.

## Deliberately not promised

Production service reliability, audio, automatic recovery, additional units,
R36S support, macOS wired output, and a Linux DRM/KMS connector remain
unverified.
