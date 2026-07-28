# Reference reproduction and Raspberry Pi parity

## Purpose

This document records the sequence that produced wired pixels on the Jetson and
defines what a Raspberry Pi must reproduce. It is an evidence map, not standing
authority to operate attached hardware. Physical work remains behind the exact
checkpoint authorization in [AGENTS.md](../AGENTS.md).

## Hardware-verified reference

Source:

```text
Jetson Orin Nano
Ubuntu 22.04 / L4T R36.4.4
USB Device Controller: tegra-xudc
USB-C device-capable port
separate DC input for Jetson power
```

Sink:

```text
one RK-X40F-family screen
screen DATA connected directly to Jetson USB-C
screen POWER connected to an independent source
```

Verified sequence:

```text
fresh UDC and stock-gadget state recorded
  -> stock NVIDIA gadget stopped inside a bounded attempt
  -> ConfigFS + FunctionFS created
  -> direct 18d1:2d00 enumerated
  -> FunctionFS bulk endpoints enabled
  -> SETR transmitted
  -> valid 316-byte SETV parsed
  -> portrait SINF transmitted
  -> Annex-B H.264 access units transmitted as VID
  -> physical video observed
  -> custom processes and gadget removed
  -> stock NVIDIA gadget restored and verified
```

Observed screen identity:

```text
product: HCT-AT01
reported version field: 2505161526
```

The generic pre-AOA identity configured but did not receive AOA requests
51/52/53 from this physical unit. Direct `18d1:2d00` is the reference behavior.

## Video milestones

The first visible fixture used portrait 720x1280 Constrained-Baseline Annex-B
H.264. A bounded sustained run delivered 50 access units and 4,577,450 H.264
payload bytes over 9.8409 seconds. The user observed continuous motion without
flicker or distortion.

Later bounded live-surface work used:

```text
Xvfb :99
640x1136 portrait
Openbox
optional Chromium kiosk
GStreamer ximagesrc -> x264enc baseline -> access-unit byte stream
10 fps
4000 kbit/s
```

The physical panel displayed a desktop, pointer, Chromium content, and video.
Those runs remain bounded observations rather than a persistent-service claim.

## Raspberry Pi parity sequence

Raspberry Pi work should change one platform layer at a time while preserving the
known protocol sequence.

### 1. Platform qualification

Record the exact Pi model, operating-system image, kernel, device tree, port,
power topology, and current UDC/ConfigFS/FunctionFS state. A port label or
marketing claim is not UDC evidence.

### 2. Stopped-state definition

Define the selected image's expected state before and after HCCAST. Some images
may have no stock gadget; others may already bind one. Cleanup checks must match
the actual Pi image rather than copying NVIDIA service names.

### 3. Direct USB identity

Present the direct Android Open Accessory identity:

```text
VID:PID 18d1:2d00
one vendor interface
bulk OUT and bulk IN endpoints
```

Success at this stage is screen-side configuration of the FunctionFS endpoints,
not pixels.

### 4. HCCAST identity gate

Send `SETR` through the reviewed session layer. Require a structurally valid
parsed `SETV`. Preserve the returned product and version as run evidence.

### 5. Known-good fixture

Send the same portrait metadata and known-good Annex-B H.264 fixture used by the
reference path. Do not introduce a new encoder, browser, or desktop until visible
fixture pixels establish protocol parity.

### 6. Physical rendering

Record exactly what appears on the screen. USB writes alone do not establish
rendering. A still frame and moving fixture are separate observations.

### 7. Cleanup

Remove only the processes, mount, and ConfigFS gadget created by the attempt.
Verify the UDC and platform return to the defined stopped state.

### 8. Live surface

After parity, adapt the reviewed 640x1136 virtual-display pipeline to the Pi's
available H.264 encoder. Measure latency, frame delivery, CPU load, temperature,
and supervised stability without changing the protocol layer simultaneously.

## Evidence labels

- Source and deterministic tests: `IMPLEMENTED` / `UNIT-TESTED`.
- One successful Pi physical run: `HARDWARE-VERIFIED` for that named Pi/image.
- A second independent unit or platform: potentially `REPRODUCED`, after review.

The Jetson result remains the sole hardware-verified source-platform result today.
