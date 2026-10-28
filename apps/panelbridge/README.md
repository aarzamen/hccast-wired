# PanelBridge

**Experimental source with bounded physical results; no qualified public installer
release yet.** PanelBridge extends the normal Raspberry Pi labwc desktop onto a
LUODIER/EBPSI wireless selfie screen using GTK4 controls, a managed virtual output,
a modified GNOME Network Displays sender and Wi-Fi Direct/Miracast. Ethernet
provides an independent recovery connection. VNC/noVNC is excluded from this path.

## Current Pi 500 scope, 2026-10-27

The selected use is extra desktop space with HDMI retained. On one Pi 500 Rev 1.0
running Debian 13 ARM64 and labwc 0.20.2, experimental `0.1.0~dev4` produced a
1280×720 virtual output to the right of HDMI. The owner confirmed the requested
browser video moving on LUODIER/EBPSI and the HDMI desktop remaining responsive.
Selected physical recordings are now available in the
[Pi 500 result record](../../docs/PI500.md) and [media gallery](../../docs/media/README.md).

The installer accepts Pi 500 and preserves unrelated residual package state;
all 60 inspected payload hashes/modes matched after installation. Automatic
casting is disabled in this setup. One manual stop/start restored home Wi-Fi and
connected again, but the first trial froze and a later closeout reported a session
timeout with network restoration unconfirmed. The freeze's cause is unresolved.
These observations prevent a reliable start/stop or unattended-use claim.

Advanced frequency controls remain restricted to their separately validated Pi 5
identity checks. Installer support for Pi 500 does not enable those controls.

## Earlier Pi 5 evidence

Normal desktop output on one Pi 5/EBPSI pairing is hardware-verified, including a
bounded 30 → 15 → 30 content-frame-rate sequence with user-confirmed motion and
geometry; negotiated video remained 720p30. An independent moving recovery card
also worked while the desktop was stopped, followed by desktop restoration.
Automatic rescue activation remains unverified.

The earlier Pi 5 deployment used dev2 with a startup repair. Automatic connection
after reboot failed until manual controller recovery. The older goal of running
with only the selfie screen from boot through shutdown remains unfinished; it is
not the current Pi 500 acceptance requirement. See [scope](../../docs/SCOPE.md).

EBPSI USB video remains unavailable. Qshot HCCAST USB and TTQ DisplayPort are
separate paths. Other host/screen combinations require their own tests. Optional
OLED rollout is deferred.

## Next acceptance work

1. Diagnose the Pi 500 freeze and session teardown timeout; qualify repeated
   manual start/stop/reconnect with Ethernet recovery and the normal desktop intact.
2. Verify reboot, screen power cycle, setting persistence and clean upgrade/removal.
   Reconcile local startup-entry edits before integrity-checked maintenance.
3. Measure sustained useful frames, loss, latency, CPU, temperature/throttling and
   input power. A short encoded-buffer sample does not qualify receiver frame
   rate or a Recommended, Performance or Battery Saver preset.
4. Keep standalone boot, automatic rescue and advanced recovery as separate
   acceptance work. Software tests and fault injection do not prove those gates.

The Pi-built development worker has private build paths in its runtime search
path and debug records. It must be rebuilt and inspected before redistribution,
with matching source and dependency notices. This publication includes source,
the pinned upstream archive and reviewed demonstration media, not development
executables or an accepted installer.

## Software checks and packaging

From the repository root:

```bash
uv sync --python 3.12 --frozen --extra dev
uv run --no-sync python -B -m pytest -p no:cacheprovider -o addopts= -q tests
uv run --no-sync python -B -m pytest -p no:cacheprovider -o addopts= -q apps/panelbridge/tests
```

Native compilation and Debian packaging require Linux distro prerequisites,
including the GUI, network and media libraries. The root Python wheel does not
install PanelBridge. Start with [the source-bundle contract](interfaces/SOURCE_BUNDLE.md),
[worker build recipe](native/wfd/build-worker.sh),
[package builder](packaging_tools/build_package.py) and
[installer builder](packaging_tools/build_installer.py). Platform skips remain
skips. Follow the root [agent contract](../../AGENTS.md) before system changes.

## Licensing and attribution

This subtree is **GPL-3.0-or-later**; the legacy HCCAST driver keeps the root MIT
license. [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
preserve the GNOME Network Displays authors and dependency notices. The owner
provided direction, hardware setup and physical acceptance. Recorded OpenAI Codex
work includes implementation and review; exact historical model/version credits
remain unrecorded. The project is not affiliated with the screen vendors.
