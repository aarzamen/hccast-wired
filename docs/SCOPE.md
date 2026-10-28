DECIDED 2026-10-27 — build-out

# Pi 500 extended display

I want to use my LUODIER / EBPSI wireless selfie screen as another monitor for my
Raspberry Pi 500. It should provide extra desktop space while the HDMI monitor
keeps working. I want to move a browser onto it and play
[this YouTube video](https://www.youtube.com/watch?v=kdemFfbS5H0). Use the existing
wireless PanelBridge path and keep Ethernet available for setup and recovery.
Preserve my normal desktop, installed applications and OLED panel. Firmware
changes, overclocking and new OLED integration are outside this work.

Completion requires a usable extended desktop, a repeatable start/stop path,
physical confirmation on the screen, and the requested video playing there.
Software checks or a connected sender alone do not establish visible playback.

## Current acceptance

Extra desktop space and video playback were physically confirmed on 2026-10-27.
Repeatable operation remains incomplete because a freeze and a later session
teardown timeout were observed. See [the Pi 500 record](PI500.md).

Qshot and TTQ were subsequently tested at the owner's request as separate
compatibility checks. Their results do not change the selected LUODIER/EBPSI
use case. The older standalone Pi 5 goal is retained as future work; removing
HDMI is not required for this scope.
