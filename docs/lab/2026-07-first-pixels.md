# First wired pixels — July 2026

## Hypothesis

An RK-X40F-family selfie monitor can accept a wired HCCAST stream from Linux when
Linux impersonates the Android accessory-side USB device expected by the screen.
The shortest candidate path is direct Android Open Accessory identity
`18d1:2d00`, followed by the APK-derived HCCAST session.

## Test topology

```text
Jetson Orin Nano, separately DC-powered
  -> USB-C device-capable port
  -> direct data-capable USB-C cable
  -> RK-X40F-family screen DATA port

independent USB power
  -> screen POWER port
```

The Jetson used Ubuntu 22.04 / L4T R36.4.4 and its Tegra USB Device Controller.
The screen packaging states 1136x640 panel resolution. The first protocol fixture
was encoded at 720x1280 portrait; later virtual-surface work used 640x1136.

## Observation

- `HARDWARE-VERIFIED`: the direct gadget enumerated as `18d1:2d00`.
- `HARDWARE-VERIFIED`: the screen configured FunctionFS bulk endpoints.
- `HARDWARE-VERIFIED`: `SETR` received a valid 316-byte `SETV`.
- `OBSERVED`: `SETV` reported product `HCT-AT01` and version field
  `2505161526`.
- `HARDWARE-VERIFIED`: portrait `SINF` was accepted.
- `HARDWARE-VERIFIED`: one 101,425-byte H.264 access unit produced the first
  visible wired test-pattern frame.
- `HARDWARE-VERIFIED`: a bounded run sent 50 access units and 4,577,450 H.264
  payload bytes over 9.8409 seconds.
- Human observation recorded continuous moving video without flicker or
  distortion.
- The generic pre-AOA personality reached FunctionFS enable but the screen did
  not emit AOA requests 51/52/53.

## Interpretation

The test proves a functioning wired Linux-to-screen HCCAST path on one Jetson
configuration and one physical screen. It establishes direct `18d1:2d00`,
FunctionFS bulk transport, HCCAST identity exchange, and physical H.264 decoding
as compatible in that topology.

It does not establish audio, automatic reconnection, reboot recovery, persistent
service reliability, Raspberry Pi compatibility, additional screen revisions,
or a Linux DRM/KMS connector.

## Implementation snapshot

The relevant implementation layers are:

```text
src/hccast_wired/gadget.py
src/hccast_wired/functionfs.py
src/hccast_wired/protocol.py
src/hccast_wired/session.py
src/hccast_wired/annexb.py
src/hccast_wired/live/
```

Raw USB logs, original observation videos, photographs, and private run evidence
remain outside the public repository. This curated record contains only the facts
needed to support the public claim.

## Next uncertainty

The next important question is portability: can a Raspberry Pi expose the same
direct gadget personality, receive the same valid `SETV`, display the known-good
fixture, and return cleanly to its expected stopped state? That is a reproduction
test of the existing architecture, not a new protocol hypothesis.

No result is currently `REPRODUCED`.
