# Changelog

## Unreleased — Fable handoff and platform reproduction

- Reconciled the public handoff around the hardware-verified Jetson direct
  `18d1:2d00` HCCAST path.
- Recorded the reviewed live virtual-display controller and supervised
  Chromium/Xvfb/video observations without promoting them to production claims.
- Made Raspberry Pi reproduction the next bounded platform target.
- Clarified that macOS is a development and diagnostic environment, not a
  hardware-verified wired output path.
- Expanded Claude Code orientation while retaining `AGENTS.md` as the
  authoritative collaboration and safety contract.

## v0.2 — WhatCable instrumentation

- Added `docs/WHATCABLE.md` with cost, capabilities, limitations, and
  topology-specific interpretation.
- Added `scripts/capture-whatcable-macos.sh` for one-command before/watch/after
  macOS captures.
- Added WhatCable capture to the README, first-run guide, and hardware test plan.
- Clarified that USB-A forces host role but also terminates downstream USB-C
  CC/PD/e-marker visibility.

## v0.1 — Initial experimental driver

- Dual direct-host and Linux gadget/AOA implementations.
- HCCAST framing, handshake, Annex-B H.264, tests, and platform scripts.
