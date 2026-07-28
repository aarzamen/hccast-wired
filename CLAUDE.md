# HCCAST Wired — Claude Code orientation

Welcome. This is an owned-hardware interoperability project for a compact,
battery-backed RK-X40F-family screen. The successful path is a local wired USB
video bridge; it does not use the vendor cloud, modify firmware, or provide
general access to another system.

## Read order

1. [AGENTS.md](AGENTS.md) is authoritative for safety, authority, evidence, and
   publication boundaries.
2. [README.md](README.md) is the technical front door.
3. [MODEL_CONTEXT.md](MODEL_CONTEXT.md) is the current short status handoff.
4. [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) explains the two USB roles and
   HCCAST transport.
5. [docs/VALIDATION.md](docs/VALIDATION.md) is the claim ledger.
6. Read the relevant source and tests before proposing a change.

## Current objective

Raspberry Pi reproduction is the active target. Reproduce the known-good Jetson
path before expanding features or revisiting protocol discovery. A second opinion
is welcome: challenge an assumption with a test or evidence and keep the claim
label proportional to what the result proves.

## Known-good reference path

Jetson Orin Nano is the hardware-verified reference:

```text
direct Android Open Accessory identity 18d1:2d00
  -> ConfigFS + FunctionFS bulk endpoints
  -> HCCAST SETR / valid SETV
  -> portrait SINF
  -> Annex-B H.264 in VID frames
  -> visible video on the physical screen
```

The generic pre-AOA personality configured, but this physical unit did not emit
AOA requests 51/52/53. Keep negotiated AOA implemented and separate from the
direct identity that actually produced pixels.

## Platform boundary

- Jetson Orin Nano: one configuration and one screen are `HARDWARE-VERIFIED`.
- Raspberry Pi 4/5: next reproduction target; not yet hardware-verified.
- macOS is a development and diagnostic controller, not a verified wired HCCAST
  display source.
- R36S and additional monitor revisions: deferred compatibility targets.
- No current result is `REPRODUCED`.

## Repository map

- `src/hccast_wired/`: protocol, transport, USB host/gadget, and live-controller
  implementation.
- `tests/`: deterministic software tests; passing tests are not hardware proof.
- `docs/REPRODUCTION.md`: verified path and Raspberry Pi parity criteria.
- `docs/TESTED_HARDWARE.md`: platform claim matrix.
- `docs/AGENT_TASKS.md`: bounded contribution briefs.
- `docs/lab/2026-07-first-pixels.md`: curated first-pixels record.

## Software verification

Use the existing isolated environment. Package installation or synchronization
requires the separate authority described in `AGENTS.md`.

```bash
uv run --no-sync pytest -p no:cacheprovider -o addopts= -q
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
python3 -m compileall -q src tests
```

## Working style

Instrument first, measure second, patch third, never guess. Work test-first,
change only the assigned files, and report exact verification output. When a
specific action is unavailable, identify that action and preserve completed work
instead of treating the whole interoperability project as blocked.

Preserve the user-selected model for the task. If the runtime changes the active
model or available capacity, report the change before substantive work resumes
and leave a precise handoff rather than silently changing scope.
