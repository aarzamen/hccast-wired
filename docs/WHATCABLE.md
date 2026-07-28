# WhatCable macOS instrumentation

WhatCable is an optional bench instrument for the macOS diagnostic branch. It
reads USB-C, USB Power Delivery, cable e-marker, active-transport, and
connected-device state exposed by Apple Silicon Macs through IOKit.

- Project: <https://www.whatcable.uk/>
- Source: <https://github.com/darrylmorley/whatcable>
- Core app and CLI: free, open source, MIT licensed.
- Pro: GBP 9.99 one-time purchase at the time of the project capture.
- Platform: Apple Silicon, macOS 14 or newer.

## Why it helped

The original Mac test produced an audible attachment indication without a usable
USB device. WhatCable helped separate:

- electrical attachment from USB enumeration;
- USB-C power/role negotiation from a data transport;
- cable claims from negotiated speed;
- direct C-to-C behavior from a USB-A topology that forced Host role;
- a persistent connection from a brief attach/reset/detach event.

The CLI's JSON, watch, and raw output complemented `system_profiler`, `ioreg`,
unified logs, and libusb.

Pro added useful Power Delivery contract/event, CC advertisement, role-swap,
fault/reset, cable VDO, and live power detail. None of those features decodes
HCCAST.

## Captured results

The project used WhatCable 1.1.9 Pro for the 2026-07-14 captures.

- `HARDWARE-VERIFIED`: direct C-to-C placed the Mac in USB Device role; no
  addressable screen peripheral enumerated.
- `HARDWARE-VERIFIED`: a USB-A adapter topology placed the Mac in USB Host role.
- `OBSERVED`: the screen transiently enumerated as `1cbe:0005` at USB 2.0 High
  Speed with one vendor interface and bulk IN/OUT endpoints.
- `HARDWARE-VERIFIED`: libusb briefly claimed the interface.
- `HARDWARE-VERIFIED`: retaining the claim did not stabilize it.
- `OBSERVED`: one bounded 20-byte `SETR` completed on bulk OUT, but bulk IN
  returned no bytes during the response window.
- `OBSERVED`: no valid `SETV`, settings, screen metadata, video, or firmware
  command occurred in the Mac diagnostic branch.

The authoritative public interpretation is in
[VALIDATION.md](VALIDATION.md). Raw captures and private post-run analyses remain
outside the public repository.

## What WhatCable cannot do

WhatCable cannot:

- force the screen or Mac into a particular USB data role;
- convert host-host wiring into host-peripheral communication;
- capture HCCAST or AOA payloads;
- make a non-enumerating device appear in libusb;
- replace a USB protocol analyzer;
- observe a Jetson or Raspberry Pi gadget exchange after the cable leaves the
  Mac.

An “attached” or “charging” result is not a HCCAST result.

## Topology interpretation

### Direct C-to-C

```text
Apple Silicon Mac USB-C -> C-to-C cable -> screen DATA
```

This topology exposes USB-C role, Power Delivery, cable, and reset behavior. On
the tested combination, it selected the Mac's Device role and no addressable
screen peripheral appeared.

### USB-A host-forcing chain

```text
Mac USB-C -> hub/adapter USB-A -> A-to-C data cable -> screen DATA
```

USB-A makes the Mac side unambiguously Host but terminates downstream USB-C
CC/Power Delivery visibility. In this topology, `system_profiler`, `ioreg`, and
libusb—not downstream e-marker data—are the decisive instruments.

### Linux gadget mode

WhatCable can pre-qualify a cable on the Mac, but Linux UDC state, FunctionFS
events, HCCAST messages, physical rendering, and cleanup evidence establish the
Jetson/Raspberry Pi result.

## Repository tools

The repository retains reviewed macOS capture helpers for reproducing diagnostic
observations:

```text
scripts/capture-macos-passive-attach.sh
scripts/capture-macos-host-claim.sh
scripts/capture-macos-setr-once.sh
scripts/capture-whatcable-macos.sh
```

### Passive attachment control

`scripts/capture-macos-passive-attach.sh` is the observation-only control for
separating ordinary power-on behavior from cable attachment.

For the visible-UI control, keep the screen externally powered and stable on the QR/setup UI
with DATA disconnected. The helper records a 30-second passive window after one
DATA attachment. It performs no interface claim and no endpoint traffic.

For the off-boot control, begin with the screen deliberately powered off and
DATA disconnected. Select the documented physical button action when launching
the 45-second observation-only window:

```bash
BOOT_ACTION=short-press scripts/capture-macos-passive-attach.sh off-boot
BOOT_ACTION=long-press-5s scripts/capture-macos-passive-attach.sh off-boot
```

This passive control does not authorize SETR, bulk endpoint traffic, a USB reset,
or any later active checkpoint.

These helpers write private run products excluded by `.gitignore`. Their presence
does not authorize physical execution. `AGENTS.md` requires a separate exact
hardware authorization for each active checkpoint.
