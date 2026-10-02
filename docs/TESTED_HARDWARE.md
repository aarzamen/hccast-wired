# Tested hardware

This matrix separates software support, diagnostic observations, and physical
interoperability. A row becomes `REPRODUCED` only after an independent second
platform or unit completes the relevant physical path.

| Source platform | Screen | Result | Claim |
|---|---|---|---|
| Jetson Orin Nano, Ubuntu 22.04 / L4T R36.4.4 | One RK-X40F-family unit | Direct `18d1:2d00`, FunctionFS, valid `SETV`, portrait H.264, visible video | `HARDWARE-VERIFIED` |
| Jetson Orin Nano, USB-A host | Qshot V2 non-Pro | `05ac:12ad`, valid SETV (`HC15B100`, `2511261024`), landscape SINF, visible 1280x720 still, independent-frame motion up to 30 seconds at 10 fps and five-second predictive playback at 30 fps, and live desktop/video for 61.7 seconds at a 30 fps target | `HARDWARE-VERIFIED` still, bounded motion and corrected landscape proportions |
| Apple Silicon MacBook Pro | Same RK-X40F-family unit | USB-C role and transient direct-host interface diagnostics; no valid `SETV` or visible wired output | `HARDWARE-VERIFIED` diagnostic facts only |
| Apple Silicon MacBook Pro | Qshot V2 non-Pro | Powered `05ac:12ad` enumeration; macOS denied interface access before any HCCAST request | Historical `OBSERVED` session findings; original temporary logs unavailable at closeout |
| Raspberry Pi 4 | Same family | Not tested as a HCCAST gadget source | Unverified target |
| Raspberry Pi 5 Model B Rev 1.1, Debian 13 aarch64 | Qshot V2 non-Pro | Host USB `05ac:12ad`; valid SETV; 150-frame fixture, one-minute desktop/video and 9,000-frame Mac-controlled desktop in 302.475735 seconds; user confirmed correct control and playback | `HARDWARE-VERIFIED`; bounded host-USB playback `REPRODUCED` from Jetson |
| Raspberry Pi 5 | RK-X40F-family unit | Not tested as a HCCAST gadget source | Unverified target |
| Raspberry Pi 400 | Same family | Host ports are not presumed device-capable; not tested | Unverified |
| R36S / RK3326 | Same family | Preserved image reports host-oriented USB configuration; no gadget test | Unverified, deferred |
| Further selfie-monitor units or revisions beyond those above | Any source | No physical test | Unverified |

## Verified Jetson topology

```text
Jetson DC power
Jetson USB-C device-capable port
  -> direct data-capable USB-C cable
  -> screen DATA port
screen POWER port
  -> independent external power
```

The known-good USB identity is direct Android Open Accessory `18d1:2d00`. The
physical screen returned a valid `SETV` with a 316-byte payload, accepted portrait `SINF`, and
rendered Annex-B H.264.

## Verified Qshot USB-A topology

`HARDWARE-VERIFIED`, 2026-09-29: Jetson USB-A host to Qshot V2 non-Pro, powered
through that cable, using `05ac:12ad` interface 0 (`ff/2a/ff`), bulk OUT `0x01`
and IN `0x81`. One landscape 1280x720 H.264 image was sent after SETV and SINF.
The user supplied a photograph showing the selected game scene with both HUD
corners and the score/stage visible. The runner exited 0 and closed the interface.
The first 50-frame predictive-video test completed transfer but was reported
static. A later five-second monochrome test sent 25 independent IDR frames at
5 fps; the user confirmed counting from 00 to 24 and the square moving right
and back. Its final frame is visible in a supplied photo. Bounded motion is
`HARDWARE-VERIFIED`. A later run used the same clip and changed only SINF to
`(1, 1280, 720, 1280, 720)`; the user confirmed a square block and unstretched
numbers. A subsequent five-second test sent 50 independent frames at 10 fps;
the user confirmed smooth motion with no stops or catches through completion.
That bounded playback is also `HARDWARE-VERIFIED`. A later 30-second test sent
300 frames at 10 fps; the user confirmed six smooth passes with no issues.
A controlled five-second run using five IDR frames plus 45 P frames at 10 fps
also played smoothly, confirmed by the user after a clean transfer. This
predictive-frame configuration is `HARDWARE-VERIFIED` for five seconds.
A later five-second predictive run sent 150 frames at 30 fps; all writes were
verified, USB closed cleanly and the user reported smooth playback. That
configuration is also `HARDWARE-VERIFIED`. Display cadence was not instrumented.
A subsequent live desktop and Chromium video run sent 1,800 frames over
61.676 seconds, targeting 30 fps and averaging 29.18 fps in transmission.
The user confirmed playback was fine until the timed stop; the held final image
initially looked like a freeze. This bounded live path is `HARDWARE-VERIFIED`.
Longer runs, rates above 30 fps, latency and reconnect behavior remain unverified.
This tests a different transport from the reference gadget path above.

## Mac diagnostic boundary

macOS established useful cable and role facts for the RK-X40F-family unit:

- C-to-C selected the Mac's USB Device role and exposed no addressable screen
  peripheral.
- A USB-A host-forcing chain exposed transient `1cbe:0005`.
- Userspace briefly owned its vendor interface.
- A bounded `SETR` received no response bytes.

The Mac has not produced a valid HCCAST handshake or physical wired video and is
not listed as a supported output source.

The separate September 27 Qshot V2 non-Pro checkpoint recorded stable powered
`05ac:12ad`, interface `ff/2a/ff`, and a missing
`com.apple.coremedia.valeria.allow` entitlement before an HCCAST request could be
sent. Offline Pro firmware inspection found matching descriptor templates; this
is not a test of Pro hardware or cross-model firmware compatibility. The original
temporary artifacts were missing at closeout. See [VALIDATION.md](VALIDATION.md)
for the historical results and evidence limits.

## Raspberry Pi gadget promotion criteria

These criteria apply to the RK-X40F gadget path, not the Qshot USB-host path.
A gadget-path Pi row advances to `HARDWARE-VERIFIED` only after the selected physical model
and software image establish:

1. a current usable UDC;
2. direct `18d1:2d00` enumeration;
3. configured FunctionFS bulk endpoints;
4. valid parsed `SETV`;
5. visible known-good H.264;
6. verified cleanup and expected stopped state.

No software test or device-tree inspection substitutes for those physical gates.

## Raspberry Pi 5 Qshot host result — 2026-10-01

Qshot V2 non-Pro host-USB playback was reproduced on Raspberry Pi 5 on
2026-10-01: 150 frames at a 30 fps sending rate, 1280x720 landscape, with user-confirmed motion and correct proportions. A later 60-second isolated desktop and sample-video test is also
`HARDWARE-VERIFIED`: 1,800 live frames in 60.044873 seconds, followed by
60 ending-card frames, with user-confirmed correct playback and clean closure.

A subsequent Mac-controlled Pi desktop session is also `HARDWARE-VERIFIED`:
9,000 live frames in 302.475735 seconds (29.75 fps average transmission),
then 60 ending frames. The user confirmed correct Mac control and smooth Qshot
output through the ending card. USB, temporary desktop processes, preview
listeners and the Mac SSH tunnel closed. Longer Pi sessions remain untested.

The same physical Qshot and prepared clip were used on both source platforms.
This does not establish compatibility with another unit or the RK-X40F gadget
route. USB closed cleanly after the fixture, with no parser residue or STOP.
