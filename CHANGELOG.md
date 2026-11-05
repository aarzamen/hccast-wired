# Changelog

## Unreleased — Pi 500 demonstration and documentation, 2026-10-28

- Track every ConfigFS resource a `gadget-handshake`/`gadget-stream` run
  creates or binds, and release only those on exit. A second start that is
  rejected because the gadget already exists no longer unbinds or removes the
  first session's gadget; explicit stale-state removal stays with
  `gadget-stop`, with a regression test against a fake configfs tree.
- Add `apps/panelbridge/packaging_tools/release.py`, which chains the source
  bundle, ARM64 package and installer builders, refuses a worker with
  development paths or debug records, cross-checks the artifacts and records
  them in `RELEASE_MANIFEST.json` with `release_ready: false`. CI runs its
  platform-independent source-only stage. No installer release is created;
  the legacy wheel still delivers only the MIT driver.
- Accept Raspberry Pi 500 in the experimental PanelBridge installer and advance
  the development package to dev4, preserving unrelated residual package state.
  Advanced frequency controls retain their separate Pi 5 restriction.
- Preserve HCCAST parser resynchronization across USB reads when a startup
  preamble arrives separately from a valid frame, with fragmentation regressions.
- Record the October 27 Pi 500 LUODIER/EBPSI extended desktop and browser-video
  demonstration, plus bounded Qshot USB motion; keep TTQ output unverified.
- Add the decided scope, Pi 500 operating/result notes, updated front-door
  documentation and owner-approved, metadata-stripped demonstration media.
- Keep the initial freeze and later teardown timeout open. No installer release,
  automatic-start acceptance, audio or sustained reliability is claimed.

## Unreleased — PanelBridge development snapshot

- Add the experimental Pi 5 normal-desktop controller, GTK controls, scoped
  network helper, rescue renderer, packaging and restoration tools.
- Preserve the modified sender source, pinned upstream archive, calibration
  accounting and reversible advanced-control source with their software tests.
- Record bounded EBPSI wireless desktop output and Qshot USB playback on Pi 5.
  Unattended startup, full recovery, calibration and uninstall acceptance remain
  unfinished; this source checkpoint is not a completed install release.
- Retain optional local OLED startup/status controls as deferred integration.
- Resume toward standalone boot-to-shutdown use and measured frame-rate,
  efficiency and performance improvements.
- Keep the legacy HCCAST MIT license and separate PanelBridge GPL-3.0-or-later
  license, notices and source boundaries.
- Keep privileged-helper fixtures in private directories so their path checks
  run on Linux without weakening the production permission checks.
- Publish passive-capture sample counts atomically so interrupted cleanup does
  not erase evidence of completed samples.

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
