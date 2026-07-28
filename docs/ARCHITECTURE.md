# Architecture

## System boundary

HCCAST Wired is a userspace compressed-video bridge for an owned
RK-X40F-family monitor. It is not an HDMI, DisplayPort, UVC, DisplayLink, or
Linux DRM/KMS implementation.

```text
isolated virtual surface
  -> screen capture
  -> H.264 Annex-B encoder
  -> HCCAST packet stream
  -> USB bulk transfers
  -> monitor decoder
  -> LCD
```

The known wired interface accepts H.264 through HCCAST. No pixel-addressable
framebuffer transport is currently known.

## Protocol layers

### HCCAST frame

Each logical packet begins with a 16-byte header:

```text
offset  size  field
0       4     total packet length, big-endian
4       4     sequence number, big-endian
8       4     command magic
12      4     flags / command argument, big-endian
16      n     payload
```

Implemented commands include `SETR`, `SETV`, `SETS`, `SINF`, `VID`, `AUD`,
`DBG`, `PING`, and `STOP`. Video uses Annex-B H.264 and preserves its start
codes. Logical HCCAST packets may exceed 64 KiB; USB writes are split into
16 KiB chunks without changing the logical frame.

### Session

The verified session sequence is:

```text
USB bulk endpoints available
  -> source sends SETR
  -> screen returns SETV
  -> source parses identity and configuration
  -> source sends portrait SINF
  -> source sends H.264 access units as VID
```

A successful USB write is transport evidence only. A valid parsed `SETV` is the
HCCAST identity gate. Visible video is the rendering gate.

## USB backend A: direct host

```text
Linux/macOS/Android = USB host
monitor             = USB peripheral
```

The APK-derived device filters are `05ac:12ad` and `abcd:0002`. The tested
physical unit instead transiently exposed `1cbe:0005` when a USB-A topology
forced the Mac into Host role.

The observed interface had:

```text
class/subclass/protocol: ff/06/50
bulk IN:                0x81
bulk OUT:               0x02
maximum packet:         512 bytes
```

macOS briefly claimed that interface, but it detached even while claimed. One
bounded `SETR` produced no response bytes and no valid `SETV`. This backend is
implemented and useful for compatible monitor variants, but it is not the
hardware-verified video path for this unit.

## USB backend B: negotiated Android Open Accessory gadget

```text
monitor      = USB host / Android accessory
Linux source = USB peripheral / gadget
```

The implemented negotiated sequence is:

```text
generic pre-AOA USB identity
  -> request 51 GET_PROTOCOL
  -> request 52 identity strings
  -> request 53 START_ACCESSORY
  -> disconnect and re-enumerate as 18d1:2d00
  -> FunctionFS bulk endpoints
  -> HCCAST session
```

The code handles requests 51/52/53 through FunctionFS. On the tested screen,
the generic identity reached FunctionFS enable but the screen did not emit those
requests. This path remains `IMPLEMENTED` and `UNIT-TESTED`, not
`HARDWARE-VERIFIED` for this unit.

## USB backend C: direct Android Open Accessory identity

```text
monitor      = USB host
Linux source = USB gadget enumerating directly as 18d1:2d00
```

This is the `HARDWARE-VERIFIED` reference path:

```text
Jetson tegra-xudc
  -> ConfigFS gadget 18d1:2d00
  -> FunctionFS bulk endpoints
  -> SETR / valid 316-byte SETV
  -> product HCT-AT01
  -> portrait SINF
  -> Annex-B H.264 VID
  -> visible physical output
```

Raspberry Pi reproduction should start here. Negotiated AOA is not the first
parity target because it was not needed for the verified unit.

## Live virtual-display controller

The reviewed live source uses an isolated display rather than the operator's
primary desktop:

```text
Xvfb :99 at 640x1136
  -> Openbox
  -> optional Chromium kiosk
  -> optional x11vnc + websockify preview
  -> GStreamer ximagesrc
  -> I420
  -> x264enc baseline, byte-stream, access-unit alignment
  -> hccast-wired gadget-stream -
```

The default model uses:

```text
640x1136 portrait
10 fps
4000 kbit/s
display :99
localhost-only controller and noVNC listeners
```

The controller source separates:

- validated desired and runtime state;
- pure command construction;
- owned subprocess lifecycle;
- stopped-state reconciliation;
- checked cleanup;
- bounded private evidence;
- optional local demo and telemetry helpers.

Bounded Jetson runs displayed an isolated desktop, Chromium content, pointer
movement, and video on the physical panel. Persistent service operation,
automatic reconnection, reboot recovery, and unattended stability remain
unverified.

## Cleanup is part of correctness

On the Jetson reference platform, the NVIDIA stock USB gadget already owns the
UDC. A custom HCCAST attempt is successful only when it:

1. discovers the current UDC rather than reusing an old value;
2. records the initial owner and service state;
3. stops the stock gadget only inside the bounded attempt;
4. owns every process and custom gadget it creates;
5. unbinds and removes the HCCAST gadget;
6. restores the stock NVIDIA gadget;
7. verifies the final owner set and postconditions.

Raspberry Pi cleanup will use the same principle but must define the expected
stopped state for the selected Pi image instead of copying Jetson-specific
service names.

## Platform roles

### Jetson Orin Nano

Reference implementation. Its USB-C device-capable port and separate DC power
avoid sharing the gadget data port with primary power.

### Raspberry Pi 4/5

Active reproduction target. The target image, USB controller, device tree, port,
and separate power arrangement must expose a usable UDC. Protocol parity should
precede live-desktop adaptation.

### macOS

Development, unit testing, USB role/cable instrumentation, and direct-host
diagnostics. No working wired HCCAST video path has been observed.

### R36S

Deferred compatibility target. The preserved ArkOS device tree reports
`dr_mode = "host"`, so an OTG label alone does not establish a gadget source
without a compatible kernel/device-tree change.

## Final fallbacks

Raw Gadget or a kernel `f_accessory` function remain possible only if a future
platform cannot express the required direct FunctionFS personality. They are not
needed by the verified Jetson path and are not current Raspberry Pi priorities.
