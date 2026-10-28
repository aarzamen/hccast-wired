# HCCAST Wired & PanelBridge

Use compact selfie screens as Linux displays. This repository contains two
experimental paths: **HCCAST Wired** sends H.264 video over USB to compatible
receivers, and **PanelBridge** extends a Raspberry Pi's normal desktop onto a
LUODIER/EBPSI screen over Wi-Fi Direct/Miracast.

**Latest milestone — 2026-10-27:** a Raspberry Pi 500 displayed a browser video
on the LUODIER/EBPSI while its HDMI monitor remained usable. A separate Qshot V2
non-Pro showed a moving test pattern over USB. These are physical demonstrations;
startup, recovery and sustained reliability still need work.

## See it working

| Pi 500 extended desktop | Qshot wired motion |
|---|---|
| ![LUODIER playing video below the Pi's separate HDMI desktop](docs/media/pi500-luodier-extended-desktop.jpg) | ![Qshot counter and moving block over wired USB](docs/media/pi500-qshot-motion.gif) |
| [Watch the 6.4-second clip](docs/media/pi500-luodier-extended-desktop.mp4) | [Watch the 9.9-second original-length clip](docs/media/pi500-qshot-wired.mp4) |

The left screen is **LUODIER/EBPSI**, not TTQ. The Qshot GIF shows a portion of a
30-second test; its loop is not a continuous reliability run. Clips are silent,
metadata-stripped derivatives of the owner's recordings. See
[media captions and provenance](docs/media/README.md) for reusable assets and
what each image establishes.

## Which screen and connection?

| Screen | Path | What is established |
|---|---|---|
| LUODIER / EBPSI | PanelBridge → Wi-Fi Direct / Miracast | Pi 500 extra desktop and video, with HDMI retained; earlier Pi 5 normal-desktop output also verified |
| 7RYMS Qshot V2 **non-Pro** | Pi USB 3 Type-A → data cable/adapter → Qshot USB-C socket labelled **UVC** | HCCAST handshake and 1280×720 moving pattern: 300 frames over 29.902 seconds, targeting 10 fps on Pi 500 |
| TTQ TF39 | Its documented input is USB-C DisplayPort Alternate Mode | Same USB host cable/adapter test produced no TTQ enumeration or picture; Pi output remains unverified |
| RK-X40F family | Jetson USB gadget → screen acting as USB host | Historical wired H.264 reference path verified on one unit; Pi gadget reproduction remains untested |

A USB-C connector or a “UVC” label does not establish a shared protocol. The
Qshot test used **HCCAST**, not standard UVC. An HDMI capture dongle converts
HDMI into USB capture data; it is not an HDMI-to-USB-C DisplayPort monitor adapter.
The [hardware matrix](docs/TESTED_HARDWARE.md) records each pairing separately.

## Current scope: an extra screen for Pi 500

The selected use is a LUODIER/EBPSI **extended desktop**, with the main HDMI
monitor still working. Ethernet stays connected for internet access and SSH
recovery while Wi-Fi carries the display. The Pi 500 demonstration used Debian
13 ARM64, labwc, and experimental PanelBridge `0.1.0~dev4`, with a 1280×720 virtual
output placed to the right of HDMI. The owner confirmed the requested YouTube
video moving and the HDMI desktop responding.

Read the [decided scope](docs/SCOPE.md) and
[Pi 500 setup and result record](docs/PI500.md). Earlier standalone, sole-monitor
boot work on Pi 5 remains a separate unfinished goal.

**Known limits:** the first Pi 500 trial froze and needed a physical restart;
the cause was not established. A later start/stop worked, but a closeout check
reported a session timeout and did not establish clean network restoration.
Automatic casting is disabled in the tested setup. Audio, repeated reconnects,
long unattended use and a fresh-user install have not passed acceptance. There
is no qualified public PanelBridge installer release yet.

## How it works

PanelBridge keeps the normal labwc desktop and creates another output:

```text
Pi desktop → managed virtual output → native H.264 sender
           → Wi-Fi Direct / Miracast → LUODIER / EBPSI
```

HCCAST Wired sends compressed video through the receiver's proprietary framing:

```text
video or virtual surface → H.264 Annex-B → HCCAST frames → USB → screen
```

The legacy driver implements framing, fragmented-stream parsing, SETR/SETV
identification, SINF geometry and VID streaming. Its USB-host backend serves the
Qshot; its ConfigFS/FunctionFS gadget backend serves the separate Jetson reference
path. It does not expose a Linux DRM/KMS monitor connector. The Pi 500 Qshot demo
was a bounded pattern sender, not an installed extended-desktop service.

See [architecture](docs/ARCHITECTURE.md),
[protocol investigation](docs/REVERSE_ENGINEERING.md), and
[PanelBridge source and status](apps/panelbridge/README.md).

## Try the software checks

Development uses `uv` and Python 3.12 or 3.13. From your checkout root:

```bash
uv sync --python 3.12 --frozen --extra dev
uv run --no-sync python -B -m pytest -p no:cacheprovider -o addopts= -q tests
uv run --no-sync python -B -m pytest -p no:cacheprovider -o addopts= -q apps/panelbridge/tests
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
uv run --no-sync hccast-wired --help
```

The root package supports Python 3.10+; PanelBridge development checks use 3.12+.
These tests exercise software behavior and packaging contracts, not physical
screens. Platform-dependent skips remain skips.

Build the legacy Python wheel and source distribution with:

```bash
uv build --out-dir dist
```

That wheel is **HCCAST Wired only**. PanelBridge additionally needs Linux distro
Python/GI, GTK4, labwc, NetworkManager, GStreamer and a compiled native worker;
installing the Python wheel does not install PanelBridge. Start with
[first-run navigation](docs/FIRST_RUN.md) and the
[PanelBridge build interfaces](apps/panelbridge/interfaces/SOURCE_BUNDLE.md).
Hardware recipes must match the actual host, screen and recovery connection.

## Earlier results and remaining work

Qshot host-USB playback was previously reproduced on Jetson and Pi 5. The Pi 5
record includes a five-minute controlled desktop/video session at a 30 fps target.
Those results do not extend the Pi 500's 30-second Qshot acceptance window.
The Jetson RK-X40F gadget result is another distinct transport. macOS remains a
development/diagnostic platform; wired Mac output is unverified.

Next work is repeatable Pi 500 startup, disconnect/reconnect and restoration,
followed by longer observed use and install/uninstall qualification. Receiver
frame rate, latency and power need measurement before performance claims.
TTQ needs a separate compatible-input test. No firmware change is needed or
proposed for these results.

## Documentation

- [Scope](docs/SCOPE.md) and [Pi 500 demonstration](docs/PI500.md)
- [Tested hardware](docs/TESTED_HARDWARE.md) and [media assets](docs/media/README.md)
- [Validation and claim ledger](docs/VALIDATION.md)
- [Reference reproduction and Raspberry Pi parity](docs/REPRODUCTION.md)
- [Roadmap](ROADMAP.md) and [changelog](CHANGELOG.md)
- [First-pixels lab record](docs/lab/2026-07-first-pixels.md)
- [Agent contract](AGENTS.md), [Claude orientation](CLAUDE.md), and [current context](MODEL_CONTEXT.md)
- [Bounded agent tasks](docs/AGENT_TASKS.md) and [contributing](CONTRIBUTING.md)
- [RK-X40F manual findings](docs/RK-X40F_MANUAL_FINDINGS.md) and [WhatCable instrumentation](docs/WHATCABLE.md)

## Provenance and licenses

This hobbyist interoperability project was developed through owned-device
experiments, factory-app analysis and bounded physical tests. It is not clinical
software and is not affiliated with the screen vendors. Raw logs, credentials,
host bindings, vendor firmware and vendor APKs are excluded. Selected owner
recordings are published with permission after metadata review.

The legacy HCCAST driver and its original documentation retain the
[MIT license](LICENSE) and [third-party notices](THIRD_PARTY_NOTICES.md).
PanelBridge has a separate [GPL-3.0-or-later license](apps/panelbridge/LICENSE)
and [notices](apps/panelbridge/THIRD_PARTY_NOTICES.md), including its modified
GNOME Network Displays source. Third-party on-screen content in demonstration
media retains its own rights; see the [media notes](docs/media/README.md).
