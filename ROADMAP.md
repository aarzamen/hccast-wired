# Roadmap

## Current technical alpha

- `HARDWARE-VERIFIED`: one Jetson Orin Nano configuration and one
  RK-X40F-family unit have a bounded direct wired H.264 path.
- `IMPLEMENTED` and `UNIT-TESTED`: the live virtual-display controller and its
  checked cleanup/evidence components exist in source.
- `OBSERVED`: isolated Chromium/Xvfb desktop interaction and supervised video
  playback were visible on the physical panel.
- No current result is `REPRODUCED`.

## Next bounded target: Raspberry Pi reproduction

Reproduce the Jetson reference path without redesigning the protocol:

1. Identify a Pi model, power topology, kernel, and port that expose a real UDC.
2. Verify direct `18d1:2d00` enumeration and FunctionFS bulk configuration.
3. Receive and parse a valid `SETV` after `SETR`.
4. Send the known-good portrait `SINF` and H.264 fixture.
5. Observe visible pixels and verify complete platform cleanup.
6. Only then adapt the isolated virtual-desktop pipeline and measure stability.

See [docs/REPRODUCTION.md](docs/REPRODUCTION.md) and
[docs/TESTED_HARDWARE.md](docs/TESTED_HARDWARE.md).

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
