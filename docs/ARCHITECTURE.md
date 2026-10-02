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
RK-X40F-family unit instead transiently exposed `1cbe:0005` when a USB-A topology
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

### Qshot V2 direct-host variant

`OBSERVED`, recorded on 2026-09-27: a separately powered Qshot V2 non-Pro exposed
`05ac:12ad` with interface `ff/2a/ff`. macOS refused its interface user client
because `com.apple.coremedia.valeria.allow` was absent. The bounded attempt
ended before sending `SETR`, so this checkpoint did not test the HCCAST protocol.

`OBSERVED`, recorded from offline Qshot V2 Pro firmware inspection: matching USB
descriptor templates and HCCast iPhone/Android mirroring components are present.
`INFERRED`: the variants may share platform components. This does not establish
non-Pro firmware compatibility or a supported host video path. The original
temporary evidence was unavailable at closeout; see [VALIDATION.md](VALIDATION.md).

`HARDWARE-VERIFIED`, 2026-09-29: the user's non-Pro Qshot subsequently responded
to the Jetson acting as USB-A host. Its configured `05ac:12ad` interface `ff/2a/ff`
uses bulk OUT `0x01` and IN `0x81`, both with 512-byte maximum packets. One SETR
received a complete 332-byte SETV frame, product `HC15B100`, version `2511261024`.
A separately authorized checkpoint then sent one landscape SINF (1280x720) and
one 472,616-byte H.264 access unit as a 472,632-byte VID message. The user's
photograph shows the selected game scene on the physical Qshot. The runner closed
the interface and exited 0. Direct-host static-image rendering is therefore
`HARDWARE-VERIFIED` on this pairing. The first 50-frame predictive-video test
completed its USB writes but was reported static. A subsequent five-second
monochrome test sent 25 independent IDR frames at 5 fps; the user confirmed the
counter and moving square. That bounded motion path is `HARDWARE-VERIFIED`.
A follow-up changed only the SINF source pair from `(720, 1280)` to
`(1280, 720)`; the user confirmed a square block and unstretched numbers. The
working landscape SINF is `(1, 1280, 720, 1280, 720)` for this source and encoder.
The CLI now orients the source pair accordingly. A subsequent five-second run
sent 50 independent IDR frames at 10 fps; the user confirmed smooth motion
through completion. That bounded playback is also `HARDWARE-VERIFIED`.
A later 300-frame run at the same rate completed smoothly for 30 seconds,
confirmed by the user and a clean transfer log. A controlled five-second run
at 10 fps then sent five IDR frames plus 45 P frames with the same pattern and
corrected SINF; the user confirmed smooth playback. That predictive-frame
configuration is `HARDWARE-VERIFIED` for five seconds. A later five-second
predictive test sent 150 frames at 30 fps with a clean close; the user reported
smooth playback. That configuration is also `HARDWARE-VERIFIED`. Display cadence
was not instrumented. A later live Xvfb/Openbox desktop with Chromium video,
encoded by software x264, sent 1,800 frames over 61.676 seconds through this
USB-A path. The user confirmed playback was fine before its timed stop. That
bounded live path is `HARDWARE-VERIFIED`, with a 30 fps target and 29.18 fps
average transmission. Longer runs and rates above 30 fps remain untested.

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

Pi 5 Qshot host-USB playback is `HARDWARE-VERIFIED` for a five-second, 150-frame
1280x720 fixture at a 30 fps sending rate, with user-confirmed correct motion
and proportions (2026-10-01). This reproduces the Jetson Qshot path. A subsequent isolated
Chromium/Xvfb desktop and public sample video also ran correctly for 60 seconds
at a 30 fps target, confirmed by the user. Encoding uses software x264; the
existing Wayland/Waveshare desktop configuration was preserved. A later
five-minute session added Mac mouse/keyboard control through loopback-only
x11vnc/noVNC and an SSH tunnel. The user confirmed correct control and Qshot
output through completion; both sides closed afterward.

Pi 4/5 RK-X40F gadget reproduction remains a separate target requiring a usable
UDC and the appropriate power/port topology; those requirements do not apply
to the verified Qshot host path.

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
