# Validation and claim ledger

This file records what each experiment proves. It intentionally separates USB
enumeration, HCCAST identity, physical rendering, and broader platform support.

## Current ledger

| Area | Evidence | Claim |
|---|---|---|
| HCCAST framing, parser, Annex-B handling, USB chunking | Deterministic software suite | `UNIT-TESTED` |
| Live controller state, subprocess ownership, cleanup, evidence handling | Deterministic software suite and independent review | `UNIT-TESTED` |
| Jetson direct `18d1:2d00` enumeration | Physical Jetson + screen checkpoint | `HARDWARE-VERIFIED` |
| Jetson `SETR -> SETV` | Valid 316-byte response from physical screen | `HARDWARE-VERIFIED` |
| Jetson static and moving H.264 output | Visible physical screen observation | `HARDWARE-VERIFIED` |
| Isolated desktop, Chromium, pointer, and video | Bounded supervised Jetson runs | `OBSERVED` |
| macOS USB role and transient interface | WhatCable, IOKit, libusb | `HARDWARE-VERIFIED` diagnostic facts |
| macOS HCCAST output | No valid `SETV`, no pixels | Not verified |
| Raspberry Pi output | No physical run | Not verified |
| Second unit or platform | No independent repetition | Not `REPRODUCED` |

## Jetson reference — 2026-07-21

Test target:

```text
Jetson Orin Nano
Ubuntu 22.04 / L4T R36.4.4
Tegra USB Device Controller
one RK-X40F-family screen
```

Validated milestones:

- `HARDWARE-VERIFIED`: ConfigFS + FunctionFS enumerated directly as Android
  accessory `18d1:2d00`.
- `HARDWARE-VERIFIED`: the screen configured both bulk endpoints and returned a
  structurally valid 316-byte HCCAST `SETV` after `SETR`.
- `OBSERVED`: parsed product `HCT-AT01`, reported version field `2505161526`,
  mirror-resolution preset 1, portrait mode, auto-revolve enabled, and full mode.
- `HARDWARE-VERIFIED`: the screen accepted portrait `SINF` for 720x1280 video.
- `HARDWARE-VERIFIED`: one 101,425-byte Annex-B access unit produced the first
  visible wired test-pattern frame.
- `HARDWARE-VERIFIED`: a sustained run sent 50 access units and 4,577,450 H.264
  payload bytes over 9.8409 seconds.
- Human observation recorded continuous moving video without flicker or
  distortion.
- `OBSERVED`: the generic pre-AOA identity reached FunctionFS enable, but the
  screen did not send AOA requests 51/52/53.

Interpretation: direct `18d1:2d00` is the hardware-verified path for this physical
unit. Negotiated AOA remains implemented but is not the observed route.

See [the curated first-pixels record](lab/2026-07-first-pixels.md).

## Live virtual-surface checkpoints — 2026-07-22

The reviewed controller composed:

```text
Xvfb :99 at 640x1136
Openbox
Chromium when kiosk mode was selected
optional localhost-only noVNC preview
GStreamer ximagesrc -> x264enc baseline
direct HCCAST gadget stream
checked process/gadget cleanup
stock NVIDIA gadget restoration
```

Bounded supervised runs displayed:

- a virtual desktop and pointer;
- local Chromium/Open WebUI content;
- corresponding noVNC and physical-panel motion;
- a browser and muted online video during an approved supervised demo.

These observations show the bridge can carry a practical virtual surface. They
do not establish unattended service reliability, automatic reconnection, reboot
recovery, audio, or long-run stability.

## Historical macOS diagnostics — 2026-07-14

Test host: Apple Silicon MacBook Pro with WhatCable Pro.

### USB role observations

- `HARDWARE-VERIFIED`: direct C-to-C placed the Mac in USB Device role; no
  addressable monitor peripheral enumerated.
- `HARDWARE-VERIFIED`: a USB-A adapter topology placed the Mac in Host role.
- `OBSERVED`: the screen transiently enumerated as `1cbe:0005`, USB 2.0 High
  Speed, interface `ff/06/50`, bulk IN `0x81`, bulk OUT `0x02`, with 512-byte
  maximum packets.

### Claim observations

- `HARDWARE-VERIFIED`: libusb briefly opened and claimed interface 0.
- `OBSERVED`: IORegistry recorded Python as exclusive owner while the interface
  existed.
- `HARDWARE-VERIFIED`: retaining the claim did not prevent detach. The device
  disappeared about 0.732 seconds after enumeration and about 0.722 seconds
  after confirmed interface open.
- Human observation recorded the screen remaining powered on its normal setup UI
  after the transient USB personality vanished.

### Bounded request observation

One separately authorized `SETR` was accepted at the USB transport layer. No IN
bytes arrived in the bounded response window, and no valid `SETV` was parsed.

Interpretation: the Mac proved role, interface, and transfer facts. It did not
prove a usable direct-host HCCAST session and did not produce visible wired video.

## Protocol fixture validation

A generated one-second, 1280x720, 5 fps Annex-B H.264 fixture produced:

```text
63 NAL units
5 access units
largest access units around 95 KiB
```

This supports two implementation requirements:

1. Annex-B start codes remain part of the encoded stream.
2. Logical HCCAST `VID` packets may exceed 64 KiB and are fragmented only at the
   USB transfer layer.

Fixture tests are `UNIT-TESTED`, not physical interoperability evidence.

## Raspberry Pi validation gate

Raspberry Pi becomes `HARDWARE-VERIFIED` only after one named Pi model/image:

1. exposes a fresh usable UDC;
2. enumerates as direct `18d1:2d00`;
3. receives FunctionFS endpoint configuration;
4. parses a valid `SETV`;
5. produces visible output from the known-good fixture;
6. cleans up and returns to its defined stopped state.

The later live virtual-surface test is a separate milestone.

## Current unknowns

- Raspberry Pi protocol and rendering parity.
- Compatibility with a second RK-X40F-family unit or revision.
- R36S gadget feasibility.
- Audio framing and playback.
- Automatic reconnect and power-cycle recovery.
- Reboot persistence.
- Long unattended thermal and stability behavior.
- Any pixel-addressable framebuffer transport.

No current result is `REPRODUCED`.
