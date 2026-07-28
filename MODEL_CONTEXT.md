# Model context

This file provides status orientation only. [AGENTS.md](AGENTS.md) is authoritative.

HCCAST Wired is an experimental userspace bridge for an owned RK-X40F-family
monitor. One Jetson Orin Nano configuration and one physical unit form the
hardware-verified reference: direct `18d1:2d00`, FunctionFS bulk endpoints, valid
`SETR -> SETV`, portrait `SINF`, and visible Annex-B H.264 video.

Raspberry Pi reproduction is the current engineering target. Parity means a
device-capable USB controller, the same direct accessory identity and HCCAST
exchange, visible known-good video, and verified cleanup. macOS is diagnostic-only:
it established USB role and transient-interface facts but no valid HCCAST session
or visible wired output.

The live virtual-display controller is implemented and independently reviewed.
Persistent service, automatic reconnection, reboot recovery, audio, Raspberry Pi,
R36S, additional screen revisions, and multi-platform reproduction remain
unverified. No current result is `REPRODUCED`.

`LiveConfig.source_user` defaults to the neutral `hccast`. Actual deployments use
an existing local account supplied through configuration.

The known wired interface accepts H.264 through HCCAST. This bridge exposes no
DRM/KMS connector, and no pixel-addressable framebuffer transport is currently
known.
