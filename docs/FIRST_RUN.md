# First-run navigation

This file is a navigation page, not an active hardware recipe. Current hardware
work is intentionally separated from software setup so historical commands cannot
be mistaken for standing authority.

## Software-only verification

Use [CONTRIBUTING.md](../CONTRIBUTING.md) for the isolated `uv` environment and
software verification commands.

## Choose the correct path

- **Pi 500 + LUODIER/EBPSI:** start with [scope](SCOPE.md) and the
  [demonstrated configuration](PI500.md). PanelBridge needs Linux desktop/network
  services and a native sender; there is no qualified public installer yet.
- **Qshot or RK-X40F:** the root `hccast-wired` Python package provides the legacy
  protocol/USB tools. Host USB and gadget USB have different hardware requirements.
- **TTQ TF39:** no working Pi output has been established in this project.

From a checkout root, `uv sync --python 3.12 --frozen --extra dev` prepares the
software environment and `uv run --no-sync hccast-wired --help` shows the legacy
CLI. `uv build --out-dir dist` builds that package, not PanelBridge. For the app,
see [its source and build interfaces](../apps/panelbridge/README.md).

## Current project status

- [README](../README.md) — technical front door.
- [Model context](../MODEL_CONTEXT.md) — short current status.
- [Validation](VALIDATION.md) — evidence and claim ledger.
- [Tested hardware](TESTED_HARDWARE.md) — platform support matrix.

## Reference and target

- [Reproduction](REPRODUCTION.md) — the hardware-verified Jetson reference path
  and Raspberry Pi parity criteria.
- [Roadmap](../ROADMAP.md) — Raspberry Pi target and work order.
- [First pixels](lab/2026-07-first-pixels.md) — curated milestone record.

## Hardware boundary

Any physical checkpoint requires a separate, explicit bounded authorization under
[AGENTS.md](../AGENTS.md). Opening this guide or running software tests does not
authorize USB, SSH, system service, privileged, or remote operations.
