# HCCAST Wired

An experimental userspace bridge that turns a compact, battery-backed
RK-X40F-family selfie monitor into a wired Linux video output.

The physical unit is not a USB-C DisplayPort monitor. It is a fixed-function
H.264 receiver. This project makes a Linux device present the Android-style USB
personality the screen expects, then sends HCCAST-framed Annex-B H.264 without
Wi-Fi, the vendor APK, or a vendor cloud service.

## Current status

| Claim | Status |
|---|---|
| Jetson Orin Nano direct `18d1:2d00` USB gadget | `HARDWARE-VERIFIED` on one configuration |
| `SETR -> SETV` from the physical RK-X40F-family unit | `HARDWARE-VERIFIED` |
| Portrait `SINF` and visible H.264 test video | `HARDWARE-VERIFIED` |
| Isolated 640x1136 Chromium/Xvfb surface on the panel | `OBSERVED` in bounded supervised runs |
| macOS direct-host output | Diagnostic observations only; no valid HCCAST session or visible wired output |
| Raspberry Pi output | Active reproduction target; not yet hardware-verified |
| R36S and additional monitor revisions | Deferred compatibility targets |
| Independent second platform or unit | Not yet `REPRODUCED` |

The known-good reference path is:

```text
Jetson USB-C device-capable port
  -> direct Android Open Accessory identity 18d1:2d00
  -> ConfigFS + FunctionFS bulk endpoints
  -> HCCAST SETR / valid SETV
  -> portrait SINF
  -> Annex-B H.264 in VID frames
  -> visible pixels on the physical screen
```

The current engineering objective is to reproduce that same path on a Raspberry
Pi before expanding features or redesigning the protocol.

## Why this hardware is useful

The screen combines a small LCD, decoder, controls, enclosure, and internal
battery in one inexpensive consumer product. It avoids the exposed driver boards,
fragile bare-panel edges, and custom enclosure work common to repurposed display
modules.

This bridge is not a conventional monitor driver. Linux renders an isolated
virtual surface, encodes it, and streams compressed video:

```text
virtual UI or desktop
  -> H.264 Annex-B encoder
  -> HCCAST framing
  -> wired USB bulk transport
  -> screen decoder and LCD
```

The bridge does not expose a Linux Direct Rendering Manager/KMS connector. The
known wired interface accepts H.264 through HCCAST, and no pixel-addressable
framebuffer transport is currently known.

## Core protocol finding

APK analysis established this factory Android pipeline:

```text
MediaProjection -> MediaCodec H.264 -> HCCAST packets -> USB -> screen decoder
```

The screen family supports two opposite USB relationships:

```text
A. Linux/Android host -> monitor USB peripheral
   APK-derived filters: 05ac:12ad or abcd:0002
   hardware-observed transient candidate: 1cbe:0005

B. monitor USB host -> Linux/Android USB gadget
   hardware-verified Jetson identity: 18d1:2d00
```

Backend B produced the verified result. The screen configured the FunctionFS
bulk endpoints and returned a structurally valid 316-byte `SETV` identifying
product `HCT-AT01`. The generic pre-AOA identity reached FunctionFS enable, but
this unit did not emit Android Open Accessory requests 51/52/53. Direct
`18d1:2d00` is therefore the reference path for this unit.

## Implemented

- Exact 16-byte HCCAST frame serialization and parsing.
- Fragmented and coalesced USB stream parsing.
- `SETR -> SETV` session handshake.
- `SETS` configuration and `SINF` screen metadata.
- `VID` streaming with Annex-B start codes preserved.
- Access-unit and NAL packetization.
- Logical video frames larger than 64 KiB.
- 16 KiB USB transfer chunking matching the factory app.
- Direct PyUSB host backend.
- ConfigFS + FunctionFS Linux gadget backend.
- Negotiated AOA requests 51/52/53.
- Direct `18d1:2d00` AOA personality.
- Bounded live virtual-display controller with checked cleanup and private
  evidence handling.
- Deterministic software tests for protocol, USB abstractions, controller
  lifecycle, cleanup, telemetry, and public-repository controls.

## Not yet verified

- Raspberry Pi hardware output.
- A second monitor unit or hardware revision.
- Automatic recovery after cable or screen-power loss.
- Reboot recovery and persistent service behavior.
- Audio.
- Long unattended stability.
- macOS as a wired HCCAST video source.
- R36S gadget operation.
- DRM/KMS integration.

## Raspberry Pi target

Raspberry Pi work is a portability exercise, not a fresh protocol-discovery
project. A successful reproduction must establish all of the following:

1. The selected Pi, kernel, device tree, port, and power topology expose a usable
   USB Device Controller.
2. The Pi enumerates directly as `18d1:2d00`.
3. The screen configures the FunctionFS bulk endpoints.
4. `SETR` receives a valid, parsed `SETV`.
5. The known-good portrait H.264 fixture produces visible pixels.
6. The run cleans up its gadget and processes and restores the Pi's expected
   stopped state.
7. Only after parity is proven does the isolated virtual-display pipeline move
   to the Pi.

See [the reproduction record](docs/REPRODUCTION.md), the
[tested-hardware matrix](docs/TESTED_HARDWARE.md), and the
[roadmap](ROADMAP.md).

## macOS result

The Mac remains useful for development, tests, USB-C/Power Delivery
instrumentation, and passive/direct-host diagnostics.

`HARDWARE-VERIFIED` observations from the tested Apple Silicon Mac:

- Direct C-to-C placed the Mac in USB Device role and exposed no addressable
  monitor peripheral.
- A USB-A host-forcing topology placed the Mac in Host role.
- The screen transiently exposed `1cbe:0005`, USB 2.0 High Speed, interface
  `ff/06/50`, bulk IN `0x81`, and bulk OUT `0x02`.
- Userspace briefly claimed the interface.
- Holding that claim did not prevent the repeatable detach.
- One bounded `SETR` was accepted at the transport layer, but no response bytes
  or valid `SETV` arrived.

These are diagnostic facts, not a functioning macOS display path. The project
does not currently send video from macOS.

## Software-only development

Read [AGENTS.md](AGENTS.md) before changing the repository. It is the binding
authority and evidence contract. [CLAUDE.md](CLAUDE.md) provides the concise
Claude Code operating map, while [MODEL_CONTEXT.md](MODEL_CONTEXT.md) carries the
short current status.

Use an isolated `uv` environment. A human preparing a checkout can provision the
development dependencies with:

```bash
uv sync --extra dev
```

Once provisioned, the non-synchronizing verification gate is:

```bash
uv run --no-sync pytest -p no:cacheprovider -o addopts= -q
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
python3 -m compileall -q src tests
```

Passing software tests support `UNIT-TESTED` claims only. They do not establish
USB enumeration, a HCCAST handshake, or physical rendering.

Hardware, remote access, package installation, and publication use the explicit
authorization boundaries in `AGENTS.md`.

## Documentation

- [Claude Code orientation](CLAUDE.md)
- [Current model context](MODEL_CONTEXT.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Validation and claim ledger](docs/VALIDATION.md)
- [Tested hardware](docs/TESTED_HARDWARE.md)
- [Reference reproduction and Raspberry Pi parity](docs/REPRODUCTION.md)
- [First-pixels lab record](docs/lab/2026-07-first-pixels.md)
- [Roadmap](ROADMAP.md)
- [Bounded agent tasks](docs/AGENT_TASKS.md)
- [Contributing](CONTRIBUTING.md)
- [Protocol reverse engineering](docs/REVERSE_ENGINEERING.md)
- [RK-X40F manual findings](docs/RK-X40F_MANUAL_FINDINGS.md)
- [WhatCable instrumentation](docs/WHATCABLE.md)

## Provenance and boundaries

The implementation was reconstructed from the behavior of an owned device,
factory application analysis, USB instrumentation, and bounded physical tests.
The repository contains original project code and documentation. It does not
include vendor APKs, firmware, decompiled vendor source, vendor manual files, raw
private logs, credentials, or metadata-bearing observation media.

This is an experimental technical alpha, not clinical software.

## License

MIT for the original code and documentation in this repository. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
