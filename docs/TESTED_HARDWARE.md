# Tested hardware

This matrix separates software support, diagnostic observations, and physical
interoperability. A row becomes `REPRODUCED` only after an independent second
platform or unit completes the relevant physical path.

| Source platform | Screen | Result | Claim |
|---|---|---|---|
| Jetson Orin Nano, Ubuntu 22.04 / L4T R36.4.4 | One RK-X40F-family unit | Direct `18d1:2d00`, FunctionFS, valid `SETV`, portrait H.264, visible video | `HARDWARE-VERIFIED` |
| Apple Silicon MacBook Pro | Same RK-X40F-family unit | USB-C role and transient direct-host interface diagnostics; no valid `SETV` or visible wired output | `HARDWARE-VERIFIED` diagnostic facts only |
| Raspberry Pi 4 | Same family | Not tested as a HCCAST gadget source | Unverified target |
| Raspberry Pi 5 | Same family | Not tested as a HCCAST gadget source | Unverified target |
| Raspberry Pi 400 | Same family | Host ports are not presumed device-capable; not tested | Unverified |
| R36S / RK3326 | Same family | Preserved image reports host-oriented USB configuration; no gadget test | Unverified, deferred |
| Any second selfie-monitor unit or revision | Any source | Not yet tested | Unverified |

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
physical screen returned a valid 316-byte `SETV`, accepted portrait `SINF`, and
rendered Annex-B H.264.

## Mac diagnostic boundary

macOS established useful cable and role facts:

- C-to-C selected the Mac's USB Device role and exposed no addressable screen
  peripheral.
- A USB-A host-forcing chain exposed transient `1cbe:0005`.
- Userspace briefly owned its vendor interface.
- A bounded `SETR` received no response bytes.

The Mac has not produced a valid HCCAST handshake or physical wired video and is
not listed as a supported output source.

## Raspberry Pi promotion criteria

A Pi row advances to `HARDWARE-VERIFIED` only after the selected physical model
and software image establish:

1. a current usable UDC;
2. direct `18d1:2d00` enumeration;
3. configured FunctionFS bulk endpoints;
4. valid parsed `SETV`;
5. visible known-good H.264;
6. verified cleanup and expected stopped state.

No software test or device-tree inspection substitutes for those physical gates.
