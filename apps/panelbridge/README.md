# PanelBridge development snapshot

**Status: incomplete experimental source, not a finished install release.**
PanelBridge aims to make a compact EBPSI-family wireless selfie screen the normal
Raspberry Pi desktop monitor from startup through daily use and shutdown. It uses
GTK4, the existing labwc session, a managed virtual output, a modified GNOME
Network Displays sender and Wi-Fi Direct/Miracast. Ethernet remains available for
independent recovery. VNC/noVNC is excluded from the display path.

## Verified scope

- **HARDWARE-VERIFIED:** normal Pi 5 desktop output on one EBPSI receiver, including
  a bounded 30 → 15 → 30 content-frame-rate sequence with user-confirmed motion,
  geometry and readable controls. Negotiated video remained 720p30.
- **HARDWARE-VERIFIED:** an independent moving recovery card while the normal
  desktop was stopped, followed by desktop restoration. Automatic rescue
  activation remains unverified.
- **OBSERVED:** the experimental installer completed with one privilege prompt;
  package and configuration readback matched. The installed version is dev2 with
  a startup-entry repair. An unattended reboot still failed to connect until a
  manual controller recovery, so startup acceptance remains open.
- **UNIT-TESTED / IMPLEMENTED:** controller, helper boundaries, packaging and
  rollback code, native frame-accounting changes, advanced-control source and
  optional local OLED integration. These labels apply to their software checks
  and source; they do not establish physical acceptance of the whole application.

EBPSI USB video is **Unavailable**. Qshot USB results are a different device and
transport. Other Pi models and Jetson video support remain untested. Optional
OLED rollout is deferred and does not block the main-screen work.

## Next acceptance work

1. Install the reviewed source changes through a safe upgrade transaction; the
   current dev2 package rejects ordinary upgrade, and source has moved ahead.
2. Verify unattended startup with only the selfie screen attached. HDMI was still
   attached during the current installed desktop result. Record what the panel
   displays before desktop connection and during shutdown.
3. Verify reboot, disconnect/reconnect, screen power cycle, mode persistence and
   timed rollback without a second monitor.
4. Connect native calibration measurements to the controller. Qualify Recommended,
   Performance and Battery Saver presets using sustained useful frames, loss,
   latency, load, temperature/throttling and input power. No preset is qualified.
5. Integrate the advanced helper and trusted observation adapter, qualify safe
   recovery before frequency trials, and complete independent recovery and clean
   uninstall acceptance. A fault injection is not an unstable-overclock result.

The Pi-built development worker has private build paths in its runtime search
path and debug records. It must be rebuilt or sanitized and checked before binary
redistribution. This snapshot publishes source and the pinned upstream archive;
it does not publish development executables or an accepted installer.

## Software checks

From the repository root, create the isolated development environment and run:

```bash
uv sync --python 3.12 --frozen --extra dev
uv run --no-sync python -B -m pytest -p no:cacheprovider -o addopts= -q tests apps/panelbridge/tests
```

Native sender compilation and Linux package checks require their distro build
prerequisites. Platform skips are reported as skips, not passing hardware checks.
Interface and build notes are under `interfaces/` and `native/wfd/`. Follow the
root [agent contract](../../AGENTS.md) before system or hardware changes.

## Licensing and attribution

This subtree is **GPL-3.0-or-later**; the legacy HCCAST driver keeps the root MIT
license. [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
preserve the GNOME Network Displays authors and dependency notices. The user
provided direction, hardware setup and physical acceptance. Recorded OpenAI Codex
work includes implementation and review; exact historical model/version credits
remain unrecorded. The project is not affiliated with the screen vendor.
