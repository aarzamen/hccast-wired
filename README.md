# HCCAST Wired

An experimental userspace bridge that turns a compact, battery-backed
RK-X40F-family selfie monitor into a wired Linux video output.

The physical unit is not a USB-C DisplayPort monitor. It is a fixed-function
H.264 receiver. This project makes a Linux device present the Android-style USB
personality the screen expects, then sends HCCAST-framed Annex-B H.264 without
Wi-Fi, the vendor APK, or a vendor cloud service.

## Current status

The repository also contains the experimental **PanelBridge** Raspberry Pi
wireless-display application under `apps/panelbridge/`. Read its
[development status and resumption steps](apps/panelbridge/README.md).
Normal desktop output on one Pi 5/EBPSI receiver is hardware-verified, while
unattended startup, operation with only the panel attached, calibrated presets,
advanced recovery and clean uninstall acceptance remain incomplete. There is no
public installer qualified for hassle-free standalone use yet.

The app/firmware static investigation resumed on **2026-09-29**. The
[current findings](docs/FIRMWARE_AND_APP_REVERSE_ENGINEERING.md) trace
monitor and remote update clients in HCCast, HCLink and 7RYMS, and recover the
official Pro image and bundled remote firmware. On the same date, the Qshot V2
non-Pro displayed one user-selected H.264 image over Jetson USB-A, confirmed by
the transfer log and a user photograph. A later five-second test established
visible motion with 25 independent H.264 frames at 5 fps: the user confirmed the
counter and moving square. Correcting the landscape source dimensions in SINF
then produced a square block and unstretched numbers, confirmed by the user.
Subsequent tests sent 50 and then 300 independent frames at 10 fps; the user
confirmed smooth playback through the full 30-second run. A controlled five-second clip using five keyframes and 45
predictive frames also played smoothly at 10 fps, with about 80% less video
data for the same pattern. A later five-second test sent 150 frames at 30 fps,
and the user confirmed smooth playback. Both predictive configurations are
`HARDWARE-VERIFIED` on this pairing. Display cadence was not instrumented;
a later live desktop and browser-video run is also `HARDWARE-VERIFIED` through
its timed stop after 61.7 seconds. It targeted 30 fps and averaged 29.18 fps in
transmission; the user confirmed playback was fine before it stopped. A subsequent Mac-controlled
desktop session completed 306.6 seconds without issues, confirmed by the user,
with the ending card and clean shutdown. That five-minute configuration is
`HARDWARE-VERIFIED`. Longer and unattended runs remain untested.

| Claim | Status |
|---|---|
| Jetson Orin Nano direct `18d1:2d00` USB gadget | `HARDWARE-VERIFIED` on one configuration |
| `SETR -> SETV` from the physical RK-X40F-family unit | `HARDWARE-VERIFIED` |
| Portrait `SINF` and visible H.264 test video | `HARDWARE-VERIFIED` |
| Isolated 640x1136 Chromium/Xvfb surface on the panel | `OBSERVED` in bounded supervised runs |
| macOS direct-host output | Diagnostic observations only; no valid HCCAST session or visible wired output |
| Qshot V2 non-Pro on macOS | `OBSERVED` USB diagnostics; interface access denied before an HCCAST request |
| Qshot V2 non-Pro on Jetson USB-A | `HARDWARE-VERIFIED` SETR → SETV, landscape SINF, visible 1280x720 still, independent-frame motion up to 30 seconds at 10 fps and five-second predictive playback at 30 fps, and live desktop/video for 61.7 seconds with a 30 fps target and corrected landscape proportions; `HC15B100`, version `2511261024` |
| Qshot V2 Pro firmware | `OBSERVED` offline inspection; compatibility with the non-Pro remains unverified |
| Raspberry Pi 5 USB host to Qshot V2 non-Pro | `HARDWARE-VERIFIED`: five-second fixture, one-minute video and five-minute Mac-controlled desktop at 1280x720, targeting 30 fps; correct output and control confirmed |
| R36S and additional monitor revisions | Deferred compatibility targets |
| Independent second platform | `REPRODUCED`: bounded Qshot host-USB playback on Jetson and Raspberry Pi 5; no second screen unit tested |

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

The RK-X40F gadget path above remains a separate, unverified Pi target. The
Qshot host-USB path has now been reproduced on Pi 5; it does not require a UDC.

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

Backend B produced the reference RK-X40F-family result. The screen configured the FunctionFS
bulk endpoints and returned a structurally valid 316-byte `SETV` identifying
product `HCT-AT01`. The generic pre-AOA identity reached FunctionFS enable, but
this unit did not emit Android Open Accessory requests 51/52/53. Direct
`18d1:2d00` is therefore the reference path for this unit.

Backend A produced the Qshot V2 non-Pro result: Jetson USB-A host to `05ac:12ad`,
valid SETV, landscape SINF, a physically visible H.264 still and independent-frame
motion at 5 and 10 fps, with 30 seconds verified at 10 fps. Five-second predictive
playback at a 30 fps sending rate is also hardware-verified. This topology does
not reproduce the reference gadget path.

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

- Raspberry Pi RK-X40F gadget output and Qshot sessions longer than five minutes.
- HCCAST video output on a second monitor unit or hardware revision.
- Automatic recovery after cable or screen-power loss.
- Reboot recovery and persistent service behavior.
- Audio.
- Long unattended stability.
- macOS as a wired HCCAST video source.
- R36S gadget operation.
- DRM/KMS integration.

## Raspberry Pi target

`HARDWARE-VERIFIED` / `REPRODUCED`, 2026-10-01: Qshot V2 non-Pro host-USB
playback was reproduced on Raspberry Pi 5 on 2026-10-01: 150 frames at a 30 fps
sending rate, 1280x720 landscape, with user-confirmed motion and correct
proportions. A later 60-second isolated desktop and sample-video test is also
`HARDWARE-VERIFIED`: 1,800 live frames in 60.044873 seconds, followed by
60 ending-card frames, with user-confirmed correct playback and clean closure.

A subsequent Mac-controlled Pi desktop session is also `HARDWARE-VERIFIED`:
9,000 live frames in 302.475735 seconds (29.75 fps average transmission),
then 60 ending frames. The user confirmed correct Mac control and smooth Qshot
output through the ending card. USB, temporary desktop processes, preview
listeners and the Mac SSH tunnel closed. Longer Pi sessions remain untested.


The separate RK-X40F gadget reproduction remains a portability exercise. It
must establish all of the following:

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

Historical `HARDWARE-VERIFIED` observations from the Apple Silicon Mac with the
RK-X40F-family unit:

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

### Qshot V2 checkpoint — 2026-09-27

`OBSERVED`, recorded during the Qshot session: the separately powered non-Pro
screen exposed `05ac:12ad`, interface `ff/2a/ff`, and stayed present throughout a
30-second observation. macOS denied interface access with the missing entitlement
`com.apple.coremedia.valeria.allow`; the single approved probe sent no HCCAST
payload. No Qshot `SETV` or wired Mac video was established.

`OBSERVED`, recorded during offline inspection: official Qshot V2 Pro firmware
1.2.7 contains matching USB descriptor templates and HCCast mirroring components.
`INFERRED`: a shared platform is plausible; firmware interchangeability is
unverified. No firmware was installed.

At session closeout, the original temporary logs and firmware files were no
longer present. These are retained session findings, not freshly repeated
measurements. See the [validation ledger](docs/VALIDATION.md).

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

The legacy HCCAST driver and its original documentation retain the root
[MIT license](LICENSE); see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
The application under `apps/panelbridge/`, including its original code, native
additions, packaging, documentation and artwork, has a separate
[GPL-3.0-or-later license](apps/panelbridge/LICENSE) and
[third-party notices](apps/panelbridge/THIRD_PARTY_NOTICES.md). Upstream components
retain their own licenses and notices.
