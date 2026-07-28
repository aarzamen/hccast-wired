# First-run navigation

This file is a navigation page, not an active hardware recipe. Current hardware
work is intentionally separated from software setup so historical commands cannot
be mistaken for standing authority.

## Software-only verification

Use [CONTRIBUTING.md](../CONTRIBUTING.md) for the isolated `uv` environment and
software verification commands.

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
