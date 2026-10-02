# Model context

This file provides status orientation only. [AGENTS.md](AGENTS.md) is authoritative.

## PanelBridge closeout snapshot — 2026-10-02

**The standalone product is incomplete.** The goal is a Raspberry Pi main screen
from startup through daily desktop use and shutdown without a second monitor.
Resume autonomously within the current approved task manifest, minimizing user
requests and improving frame rate, efficiency and performance through measured
comparisons. Private host bindings, approvals, plans and raw evidence are excluded
from this public snapshot. See [PanelBridge status](apps/panelbridge/README.md).

The current approach is wireless-first Pi 5, GTK4, normal labwc desktop and
Wi-Fi Direct/Miracast with Ethernet for recovery. EBPSI USB video is unavailable;
active USB protocol work is deferred. Qshot USB evidence describes a different
transport and cannot establish EBPSI compatibility.

**HARDWARE-VERIFIED:** one Pi 5/EBPSI pairing displayed the normal desktop with
user-confirmed motion, proportions and readable controls. A bounded 30 → 15 → 30
content-fps sequence worked; negotiated video stayed 720p30. An independent moving
recovery card also worked while the desktop was stopped, followed by restoration.
Neither result proves unattended recovery, headless-only boot or long-term use.

**OBSERVED:** the experimental fresh installer completed with one privilege
prompt and matching package/configuration readback. The installed version remains
`0.1.0~dev2` plus a startup-entry repair. A read-only closeout check found the helper
and normal-desktop sender running, rescue inactive and no named test harness.
This is process state, not a fresh panel observation. Automatic connection after
reboot previously failed until manual controller recovery. HDMI was still attached.

**IMPLEMENTED / UNIT-TESTED:** source adds bounded Ethernet readiness before
startup, precise network restoration errors, optional OLED controls, native frame
accounting and reversible advanced-control logic. Custom GstMeta frame carriage
replaced the disproved OFFSET approach and passed generated Pi pipelines. Full
calibration/controller integration and qualified presets remain pending. The
advanced observer exists in source and fixture tests; deployment, trusted safety
facts and physical clock/recovery qualification remain incomplete. New source is
not installed. A development native worker must have its private runtime/debug
paths removed and be checked before binary redistribution.

On resumption, complete a safe upgrade, unattended boot with only the panel,
reconnect/power-cycle/mode persistence, timed rollback, automatic rescue and clean
uninstall. Measure sustained useful frames, loss, latency, CPU load, temperature,
throttling and input power before selecting performance changes. The user reports
an official 27 W supply, active fan and available power meter; those reports do not
qualify a clock trial or preset. Record panel behavior before connection and at
shutdown. No further storage image write is planned.

The user owns five OLED modules and intends one per Pi/Jetson, each controlling
its own host. Optional startup/control integration is software-tested; rollout and
portability are deferred. Preserve that work without making it a completion gate.
No GPIO or live boot settings changed. Existing HCCAST results below retain their
device-specific historical scope.

## Legacy HCCAST context

App/firmware static investigation resumed on 2026-09-29. The
[current findings](docs/FIRMWARE_AND_APP_REVERSE_ENGINEERING.md) cover HCCast
3.3.0, HCLink 1.3.5, 7RYMS 0.0.3, SETV update metadata, UPGI/UPG uploads,
and recovered official Pro firmware plus bundled remote firmware.
Qshot static-image output and bounded motion are hardware-verified on Jetson
USB-A, using independent frames at 5 and 10 fps. The user confirmed smooth
completion of a 30-second run at 10 fps. A separate controlled test
verified corrected landscape proportions with oriented source dimensions.
Five-second predictive-frame playback at 10 and 30 fps is now also hardware-verified
with the same monochrome source and corrected dimensions.
A subsequent live desktop and browser-video run is hardware-verified for
61.7 seconds, targeting 30 fps and averaging 29.18 fps in transmission.
An earlier predictive-frame test was reported static; its cause remains unresolved.
Raspberry Pi 5 Qshot host-USB fixture playback is now hardware-verified; see the
October 1 result below. Original machine records remain private.

HCCAST Wired is an experimental userspace bridge for an owned RK-X40F-family
monitor. One Jetson Orin Nano configuration and one physical unit form the
hardware-verified reference: direct `18d1:2d00`, FunctionFS bulk endpoints, valid
`SETR -> SETV`, portrait `SINF`, and visible Annex-B H.264 video.

Raspberry Pi reproduction of the separate RK-X40F gadget path remains parked. Parity means a
device-capable USB controller, the same direct accessory identity and HCCAST
exchange, visible known-good video, and verified cleanup. macOS is diagnostic-only:
it established USB role and transient-interface facts but no valid HCCAST session
or visible wired output.

The separate Qshot V2 non-Pro session recorded stable powered `05ac:12ad`
enumeration and a macOS `com.apple.coremedia.valeria.allow` denial before any
HCCAST payload was sent. Offline Pro firmware inspection found matching USB
templates and HCCast components. Those observations suggest a shared platform;
non-Pro firmware compatibility remains unverified. Original September 27 USB
logs remain missing. The Pro firmware was reacquired on September 29 with the
same historical hash, and its container/uImage CRCs were freshly verified. The
non-Pro unit's SETV metadata URL was recovered in the later USB-A checkpoint below. Static APK analysis mapped
7RYMS's JieLi BLE update service and device authentication, plus HCLink's active
heartbeat and 1,024-byte firmware chunks. These are `OBSERVED` app-code findings;
neither updater was exercised on hardware.

A user-approved Jetson identification checkpoint then captured `1cbe:0005`,
interface `ff/06/50`, with bulk endpoints `0x02` / `0x81`. It stopped at the
device-node access precheck before claiming or sending SETR. The node disappeared,
and kernel logs show repeated enumeration errors and disconnects predating the
probe. A permissions problem is not yet established. The user then added external
power and kept C-to-C. No known monitor identity appeared in 61 passive samples;
kernel reads showed Jetson `device` role with stock `l4t` bound but unconfigured.
The user ran the approved direct `18d1:2d00` gadget checkpoint after entering the
sudo password locally. Gadget creation/binding succeeded, but the 10-second wait
received `BIND` without `ENABLE`; no SETR was attempted. Stock `l4t` restoration
was independently verified, with the test gadget and mount gone.

The user then connected USB-A without external power and requested another try.
`OBSERVED`: `05ac:12ad`, interface `ff/2a/ff`, OUT `0x01` / IN `0x81`, remained
present in all 13 samples over six seconds. After entering sudo credentials
locally, the user ran one SETR and received a valid 332-byte SETV (316-byte payload):
`HC15B100`, version `2511261024`. Raw bytes were independently parsed; this USB-A
identification path is `HARDWARE-VERIFIED`. The probe exited and the device stayed
present. Its private-address metadata URL matches a cached Pro firmware template,
but that establishes neither reachability nor firmware compatibility.

The separately approved single-frame checkpoint completed at 11:00:48–49 UTC.
One fresh SETR received valid SETV, followed by one landscape SINF and one
472,616-byte H.264 access unit at 1280x720. All writes returned; the runner closed
USB and exited 0. The user's photograph shows the supplied game scene, both
characters and full HUD on the Qshot. This direct-host static-image path is
`HARDWARE-VERIFIED`. The process is gone and the same USB address remains present.
Evidence and the completed single-use runner are private. The later motion test
ran at 11:32:10–16 UTC: 50 VID frames at 10 fps over 4.905 seconds; exit 0, no STOP,
USB close returned. All outgoing-message hashes match the prepared clip, which
decodes locally into 50 distinct frames. The user reported a static display and
confirmed no rotation or scale change. Their photo shows a cropped portion of
the pattern with its counter out of view. The cause is unresolved; USB completion
does not establish playback. Both completed markers remain intact.

The separate color diagnostic completed at 11:46:26–30 UTC: 15 independently
decodable frames at 5 fps over 2.802 seconds, same screen information, exit 0,
USB closed, no STOP. The user reported multiple visual states. That supports
visible image updates, while smooth motion and correct cropping remain unverified.
Exact color names are not a reliable acceptance criterion for this session.
The five-second monochrome checkpoint completed at 12:03:41–47 UTC: 25
independent IDR frames at 5 fps, 4.804 seconds of writes, valid SETV sequence 4,
exit 0 and USB closed. All 27 message hashes match the prepared sequence. The
user confirmed counter 00 through 24 and square movement as described; their
photo shows final 24 with the marker at the left. This bounded motion result is
`HARDWARE-VERIFIED`. The graphic appears enlarged and horizontally stretched;
correct scaling, higher rates and sustained playback remain unverified. The
process is gone and the USB address is unchanged. Evidence and completed markers
are private.

Static HCCast 3.3.0 tracing found a landscape SINF mismatch: the app swaps both
the encoder and source dimension pairs, while our completed Qshot tests sent
`(1, 1280, 720, 720, 1280)`. The geometry checkpoint completed at
12:22:42–48 UTC with `(1, 1280, 720, 1280, 720)` and the exact same 25-frame
clip at 5 fps. SETV sequence 5 was valid; all 27 message hashes matched, USB
closed, exit was 0 and the process was absent afterward. The user confirmed
the animation, a square block and unstretched numbers. This correction is
`HARDWARE-VERIFIED` for this pairing. The CLI now follows the corrected source
order on both Mac and Jetson, with regression coverage for portrait and landscape
dimensions. All 13 geometry cases pass on the Jetson Python 3.10 runtime.

`OBSERVED`: the five-second 10 fps checkpoint completed at 13:55:29 UTC,
sending all 50 independent IDR frames over 4.904 seconds and closing cleanly.
All 52 outgoing message hashes matched the prepared sequence; exit was 0.
The user requested another viewing. The identical rerun completed at 13:59:21 UTC:
50 frames over 4.903 seconds, SETV sequence 7, all 52 outgoing hashes verified,
no STOP or parser residue, USB closed and exit 0. The user confirmed smooth
progression with no stops or catches through completion. Five-second
independent-frame playback at 10 fps is now `HARDWARE-VERIFIED` on this pairing.
Rates above 10 fps remain untested; the longer run follows below.
`UNIT-TESTED`: the original checkpoint passed 120 relevant checks.

A 30-second checkpoint used six byte-identical
copies of the verified clip, 300 frames at 10 fps in one connection, and the
same landscape dimensions. The counter repeats 00 through 49 six times.
`UNIT-TESTED`: 122 relevant checks passed, including timed transmission and
the 300-frame limit. All 300 frames decode without errors; each cycle matches
the original. Jetson hashes, import and passive descriptors matched. The runner
has a 40-second deadline plus two-second termination grace.
`HARDWARE-VERIFIED`: the run completed at 14:18:30 UTC, with 300 frames over
29.903 seconds, SETV sequence 8, all 302 outgoing hashes verified, no STOP or
parser residue, USB closed and exit 0. The user reported six smooth passes
with no issues. Playback beyond 30 seconds remains untested.

A controlled predictive-frame checkpoint used
the same five-second monochrome source at 10 fps and corrected SINF, encoded
as five IDR frames plus 45 P frames. Recreating the independent-frame baseline
from that source matched its original hash exactly. The new clip is 50,575
bytes, about 20% of the baseline, and its decoded binary pattern matches all
50 original frames. `UNIT-TESTED`: 122 relevant checks passed; Jetson hashes,
import, bounds and passive descriptors matched. It retains one SETR, one SINF,
50 VID messages and the 12-second deadline. `HARDWARE-VERIFIED`: the run
completed at 14:47:46 UTC, sending all 50 frames over 4.901 seconds, with
SETV sequence 9, all 52 outgoing hashes verified, no STOP or parser residue,
USB closed and exit 0. The user reported smooth playback. Predictive runs
beyond five seconds remain untested; the earlier static report's cause remains
unresolved.

`UNIT-TESTED`: the user chose a five-second 30 fps checkpoint next. It was staged
with 150 frames: five IDR frames and 145 P frames, retaining one keyframe per
second and the corrected landscape dimensions. The square follows the same
trajectory in finer steps; counter 00 through 49 holds each numeral for three
frames. Every third source frame matches the verified 10 fps source. All 150
decoded binary patterns match the new source, and 122 relevant checks passed.
Jetson import, file/source hashes, message bounds and passive descriptors match.
The runner permits one SETR, one SINF and 150 VID messages, with a 12-second
deadline plus two-second termination grace. `HARDWARE-VERIFIED`: the run
completed at 15:17:03 UTC, sending all 150 frames over 4.967 seconds, with
SETV sequence 10, all 152 outgoing hashes verified, no STOP or parser residue,
USB closed and exit 0. The user reported smooth playback. This verifies the
five-second configuration at a 30 fps sending rate; display cadence was not
instrumented. Longer predictive playback remains untested.

A user-approved one-minute Qshot live desktop/video checkpoint was prepared at
1280x720 and 30 fps, with at most 1,800 video messages. It uses an isolated
Xvfb/Openbox desktop, Chromium playing a public sample video beside a live clock,
and software H.264 encoding through the existing USB-A host session. A source-only
Jetson check encoded 450 frames in 15.079 seconds; all frames decode, the browser
reported continuing video playback with zero dropped frames, and source processes
closed. The browser stays unprivileged; the USB worker uses the user's interactive
sudo authorization. `OBSERVED`: the physical run completed at 15:45:17 UTC,
sending all 1,800 frames over 61.676 seconds (29.18 fps average). All 1,802 outgoing
hashes match the saved video and framing; SETV sequence 11 was valid, USB closed,
exit was 0 and all owned source processes stopped. The browser reported zero
dropped frames, and the transmitted video decodes through its last frame.
The user initially reported a frozen screen, then clarified: "No, it was fine
before it stopped." The still image followed the planned end. This live desktop
and video checkpoint is now `HARDWARE-VERIFIED` on this pairing. The final source
frame shows counter 61 and clock 08:45:17. Physical confirmation comes from the
user report; display cadence and end-to-end latency were not instrumented.
Original machine evidence and both user reports are retained privately.

A reusable private Qshot launcher is now `IMPLEMENTED` and `UNIT-TESTED`.
It retains the one-minute, 1280x720/30 fps live setup, supports Start/Stop and
Ctrl+C, saves each run separately, prevents overlapping starts, and sends a
high-contrast "Session ended" card for two seconds before closing USB. The
timed ending is now `HARDWARE-VERIFIED`: the user reported completion and a good
result. The run completed at 20:11:24 UTC with 1,800 live frames over 61.613 seconds,
then 60 ending-card frames. All 1,862 outgoing hashes match; SETV sequence 13,
USB closure, exit 0 and source cleanup were verified. The manual-stop transition
and repeated launcher sessions remain unverified. All 164 relevant software
checks passed before staging. Private staging and verification records are under
the September 29 analysis directory, in `qshot-launcher-20260929` and
`jetson-qshot-launcher-20260929T201011Z`.


Mac browser control is now `IMPLEMENTED` and `UNIT-TESTED` through the private
launcher's `--control` mode. It reuses installed x11vnc/noVNC, binds only Jetson
loopback ports 5907 and 6087, and reaches the Mac through SSH forwarding. This mode
has a five-minute target, at most 9,000 live frames plus 60 ending frames, and a
320 MiB payload limit; the original one-minute mode is unchanged. `OBSERVED` in
a desktop-only check: Mac keyboard events reached the isolated desktop, YouTube
search loaded visibly, 1,800 source frames encoded in 60.102 seconds and decoded
without errors, and preview processes/ports closed. No USB or sudo was used.
All 168 relevant software checks passed. The combined five-minute Mac-control
and Qshot session is now `HARDWARE-VERIFIED`: it completed at 20:32:43 UTC,
sending 9,000 live frames over 306.586 seconds (29.36 fps average transmission),
followed by 60 ending-card frames. The user reported completion with no issues.
All 9,062 outgoing hashes were independently verified; SETV sequence 14 identifies
the same product/version. All 9,060 frames decode without errors. USB closed,
exit was 0, source processes stopped, and the preview ports and X socket closed.
Manual Stop/Ctrl+C and sessions longer than this remain unverified. YouTube
playback details were not separately confirmed; the landing-page video metric
does not measure playback after navigating away. Original machine records and
the separate user confirmation are retained privately.

The live virtual-display controller is implemented and independently reviewed.
Persistent service, automatic reconnection, reboot recovery, audio, Pi
RK-X40F gadget output, R36S, and additional screen revisions remain unverified.
Bounded Qshot host-USB playback is `REPRODUCED` on Pi 5; see below.

`LiveConfig.source_user` defaults to the neutral `hccast`. Actual deployments use
an existing local account supplied through configuration.

The known wired interface accepts H.264 through HCCAST. This bridge exposes no
DRM/KMS connector, and no pixel-addressable framebuffer transport is currently
known.

Raspberry Pi 5 Qshot result, 2026-10-01:

Qshot V2 non-Pro host-USB playback was reproduced on Raspberry Pi 5 on
2026-10-01: 150 frames at a 30 fps sending rate, 1280x720 landscape, with user-confirmed motion and correct proportions. A later 60-second isolated desktop and sample-video test is also
`HARDWARE-VERIFIED`: 1,800 live frames in 60.044873 seconds, followed by
60 ending-card frames, with user-confirmed correct playback and clean closure.

A subsequent Mac-controlled Pi desktop session is also `HARDWARE-VERIFIED`:
9,000 live frames in 302.475735 seconds (29.75 fps average transmission),
then 60 ending frames. The user confirmed correct Mac control and smooth Qshot
output through the ending card. USB, temporary desktop processes, preview
listeners and the Mac SSH tunnel closed. Longer Pi sessions remain untested.


The Pi is Model B Rev 1.1, 16 GB, aarch64 Debian 13, kernel
`6.18.34+rpt-rpi-2712`, Python 3.13.5. The Qshot enumerated as `05ac:12ad`,
interface 0 `ff/2a/ff`, OUT `0x01` / IN `0x81`, and returned SETV product
`HC15B100`, version `2511261024`. All 152 outgoing messages matched the
prepared sequence. The 150 frames took 4.966929 seconds; exit 0, no STOP,
zero parser residue, and USB closure returned. The user confirmed: "Yes, it
works correctly, and the proportions are correct." The single-use runner has
a completed marker. The later live test completed at 14:16:40 UTC; the user
reported, "Yes, it ran correctly." All 1,862 message hashes matched and all
1,860 captured frames decoded without errors. USB closed with no STOP or parser
residue; temporary desktop processes stopped. An earlier identical run completed
but was missed by the user. The later five-minute Mac-control result is recorded above.
The existing Waveshare desktop configuration was not changed.


A private Mac launcher now provides a Start/Stop/Status menu for the verified
five-minute Pi Qshot session. It opens a regular Chrome control window, chooses
a fresh loopback port for each SSH tunnel, checks the remote launcher hash and
closes its own local processes on completion or failure. The software-only
end-to-end check completed in 60.014139 seconds with no USB attempt, no remaining
preview listeners/processes and no remaining Mac tunnel. Eight lifecycle tests
passed; the full suite reported 391 passes and the same three existing AGENTS
contract-text failures. The user subsequently ran the Mac launcher and reported, "ok, it ran great."
Its saved log reports SESSION_ENDED at 14:45:43 UTC, USB return code 0, browser
video playback, and desktop cleanup. This user-operated launcher path is now
hardware-verified; the Pi streaming implementation is unchanged. Manual early
Stop remains software-tested rather than physically verified.
