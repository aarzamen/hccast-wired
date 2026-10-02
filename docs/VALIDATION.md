# Validation and claim ledger

This file records what each experiment proves. It intentionally separates USB
enumeration, HCCAST identity, physical rendering, and broader platform support.

## Current ledger

| Area | Evidence | Claim |
|---|---|---|
| HCCAST framing, parser, Annex-B handling, USB chunking | Deterministic software suite | `UNIT-TESTED` |
| Live controller state, subprocess ownership, cleanup, evidence handling | Deterministic software suite and independent review | `UNIT-TESTED` |
| Jetson direct `18d1:2d00` enumeration | Physical Jetson + screen checkpoint | `HARDWARE-VERIFIED` |
| Jetson reference `SETR -> SETV` | Valid SETV with 316-byte payload from physical screen | `HARDWARE-VERIFIED` |
| Jetson static and moving H.264 output | Visible physical screen observation | `HARDWARE-VERIFIED` |
| Isolated desktop, Chromium, pointer, and video | Bounded supervised Jetson runs | `OBSERVED` |
| macOS USB role and transient interface | WhatCable, IOKit, libusb | `HARDWARE-VERIFIED` diagnostic facts |
| macOS HCCAST output | No valid `SETV`, no pixels | Not verified |
| Qshot V2 non-Pro powered USB identity and macOS access denial | September 27 session record; original temporary logs missing at closeout | Historical `OBSERVED` findings; not rerun |
| Qshot V2 Pro firmware structure and matching USB templates | September 27 offline inspection record; original temporary binaries missing at closeout | Historical `OBSERVED` findings; not reverified |
| Qshot V2 non-Pro on Jetson USB-A | One SETR → valid 332-byte SETV; saved raw bytes independently parsed | `HARDWARE-VERIFIED` identification only |
| Qshot V2 non-Pro static H.264 output on Jetson USB-A | One landscape SINF + one 1280x720 VID; successful log and user photograph of matching scene | `HARDWARE-VERIFIED` single-frame rendering |
| Qshot V2 non-Pro initial predictive-video test on Jetson USB-A | 50 frames written in 4.905 seconds; user reported static display, photo shows cropped pattern | `OBSERVED` transfer and static-display report; that checkpoint did not verify motion |
| Qshot V2 non-Pro independent-frame motion on Jetson USB-A | 25 IDR frames at 5 fps; user confirmed counter 00 through 24 and moving square, photo shows final frame | `HARDWARE-VERIFIED` bounded five-second motion |
| Qshot V2 non-Pro corrected landscape proportions | Same 25-frame clip; only SINF source dimensions changed to 1280x720; user confirmed a square block and unstretched digits | `HARDWARE-VERIFIED` for this landscape test |
| Qshot V2 non-Pro independent-frame motion at 10 fps | 50 IDR frames sent over 4.903 seconds; user confirmed smooth progression through completion without stops or catches | `HARDWARE-VERIFIED` bounded five-second playback |
| Qshot V2 non-Pro 30-second playback at 10 fps | 300 IDR frames sent over 29.903 seconds; user confirmed six smooth passes with no issues | `HARDWARE-VERIFIED` bounded 30-second playback |
| Qshot V2 non-Pro controlled predictive-frame playback | Five IDR frames plus 45 P frames at 10 fps; all writes verified, clean close and user reported smooth motion | `HARDWARE-VERIFIED` bounded five-second playback |
| Qshot V2 non-Pro predictive playback at 30 fps | Five IDR frames plus 145 P frames sent over 4.967 seconds; all writes verified, clean close and user reported smooth motion | `HARDWARE-VERIFIED` bounded five-second playback at a 30 fps sending rate |
| Qshot V2 non-Pro live desktop and browser video | 1,800 frames over 61.676 seconds; 30 fps target, 29.18 fps average transmission; user confirmed playback was fine until the timed stop | `HARDWARE-VERIFIED` bounded live playback |
| Qshot V2 non-Pro longer live runs or Pro firmware compatibility | No longer-duration or compatibility test | Not verified |
| Raspberry Pi 5 Qshot host-USB fixture | 150 frames in 4.966929 s; all writes verified, clean USB close; user confirmed correct output and proportions | `HARDWARE-VERIFIED` for five seconds at a 30 fps sending rate |
| Raspberry Pi 5 Qshot live desktop/video | 1,800 live frames in 60.044873 s, 60 ending frames, clean closure; user confirmed correct playback | `HARDWARE-VERIFIED` bounded one-minute run |
| Raspberry Pi 5 Mac-controlled Qshot desktop | 9,000 live frames in 302.475735 s, 60 ending frames, user confirmed control and smooth completion | `HARDWARE-VERIFIED` bounded five-minute run |
| Raspberry Pi RK-X40F gadget output | No physical run | Not verified |
| Second source platform, same Qshot unit | Same bounded fixture displayed on Jetson and Raspberry Pi 5 | `REPRODUCED` for this host-USB fixture only |

## Jetson reference — 2026-07-21

Test target:

```text
Jetson Orin Nano
Ubuntu 22.04 / L4T R36.4.4
Tegra USB Device Controller
one RK-X40F-family screen
```

Validated milestones:

- `HARDWARE-VERIFIED`: ConfigFS + FunctionFS enumerated directly as Android
  accessory `18d1:2d00`.
- `HARDWARE-VERIFIED`: the screen configured both bulk endpoints and returned a
  structurally valid 316-byte HCCAST `SETV` after `SETR`.
- `OBSERVED`: parsed product `HCT-AT01`, reported version field `2505161526`,
  mirror-resolution preset 1, portrait mode, auto-revolve enabled, and full mode.
- `HARDWARE-VERIFIED`: the screen accepted portrait `SINF` for 720x1280 video.
- `HARDWARE-VERIFIED`: one 101,425-byte Annex-B access unit produced the first
  visible wired test-pattern frame.
- `HARDWARE-VERIFIED`: a sustained run sent 50 access units and 4,577,450 H.264
  payload bytes over 9.8409 seconds.
- Human observation recorded continuous moving video without flicker or
  distortion.
- `OBSERVED`: the generic pre-AOA identity reached FunctionFS enable, but the
  screen did not send AOA requests 51/52/53.

Interpretation: direct `18d1:2d00` is the hardware-verified path for this physical
unit. Negotiated AOA remains implemented but is not the observed route.

See [the curated first-pixels record](lab/2026-07-first-pixels.md).

## Live virtual-surface checkpoints — 2026-07-22

The reviewed controller composed:

```text
Xvfb :99 at 640x1136
Openbox
Chromium when kiosk mode was selected
optional localhost-only noVNC preview
GStreamer ximagesrc -> x264enc baseline
direct HCCAST gadget stream
checked process/gadget cleanup
stock NVIDIA gadget restoration
```

Bounded supervised runs displayed:

- a virtual desktop and pointer;
- local Chromium/Open WebUI content;
- corresponding noVNC and physical-panel motion;
- a browser and muted online video during an approved supervised demo.

These observations show the bridge can carry a practical virtual surface. They
do not establish unattended service reliability, automatic reconnection, reboot
recovery, audio, or long-run stability.

## Historical macOS diagnostics — 2026-07-14

Test host: Apple Silicon MacBook Pro with WhatCable Pro.

### USB role observations

- `HARDWARE-VERIFIED`: direct C-to-C placed the Mac in USB Device role; no
  addressable monitor peripheral enumerated.
- `HARDWARE-VERIFIED`: a USB-A adapter topology placed the Mac in Host role.
- `OBSERVED`: the screen transiently enumerated as `1cbe:0005`, USB 2.0 High
  Speed, interface `ff/06/50`, bulk IN `0x81`, bulk OUT `0x02`, with 512-byte
  maximum packets.

### Claim observations

- `HARDWARE-VERIFIED`: libusb briefly opened and claimed interface 0.
- `OBSERVED`: IORegistry recorded Python as exclusive owner while the interface
  existed.
- `HARDWARE-VERIFIED`: retaining the claim did not prevent detach. The device
  disappeared about 0.732 seconds after enumeration and about 0.722 seconds
  after confirmed interface open.
- Human observation recorded the screen remaining powered on its normal setup UI
  after the transient USB personality vanished.

### Bounded request observation

One separately authorized `SETR` was accepted at the USB transport layer. No IN
bytes arrived in the bounded response window, and no valid `SETV` was parsed.

Interpretation: the Mac proved role, interface, and transfer facts. It did not
prove a usable direct-host HCCAST session and did not produce visible wired video.

## Qshot V2 investigation — 2026-09-27

This checkpoint used a different screen: a 7RYMS Qshot V2 **non-Pro** connected
to the Mac by USB-C with separate external power. It does not replace the
RK-X40F-family reference result.

Recorded `OBSERVED` findings:

- With external power, `05ac:12ad` remained present in all 31 samples over
  30 seconds. The product string was `Lightning Digital AV Adapter`; that is a
  self-reported descriptor, not hardware-origin proof. Interface 0 was `Nero`,
  class/subclass/protocol `ff/2a/ff`, with two endpoints.
- No external Mac display was reported. The earlier power configuration showed
  a repeatedly disappearing `1cbe:0005` personality; external power was
  associated with stable runtime enumeration.
- The single approved 20-byte `SETR` attempt failed during interface access.
  The kernel reported the missing `com.apple.coremedia.valeria.allow`
  entitlement. **Zero HCCAST payload bytes were sent**, and no response file was
  created. This was an access-policy result, not a failed HCCAST response.
- Library cleanup completed. All 31 post-attempt samples retained the same
  target registry identity. These were bounded observations on September 27,
  not a statement about the device's current connection or power state.
- The user confirmed iPhone USB-C mirroring, external-power pass-through
  charging, and Bluetooth pairing with the remote. The manual's 4K/60 claim
  concerns phone recording compatibility, not the 480x800 panel resolution.

### Official Pro firmware inspection

After separate approval for the different model's archive, the
[official Qshot V2 Pro package](https://www.7ryms.com/download_details/13.html)
was downloaded and inspected offline. The English and Chinese catalogs checked
in that session listed only a Pro package; this catalog result has not been
refreshed at closeout.

Recorded `OBSERVED` findings:

- Version `1.2.7 20260604`; page publication date July 6, 2026.
- One ZIP member, 3,470,959 bytes; an HCRTOS/FreeRTOS image with board tag
  `HC15B100` and embedded board label `hc1512a@dbB100`.
- LZMA decompression produced an 8,259,608-byte `hcscreenhybrid` application.
  ZIP member integrity and uImage header/payload CRC checks passed.
- Static USB templates contained `05ac:12ad` and `ff/2a/ff`, matching the
  measured non-Pro identifiers. HCCast IUM/AUM mirroring components were present.
- Updater diagnostics included board-product, version, and CRC checks. No
  conclusion about cryptographic signing or modified-firmware acceptance was
  established.

`INFERRED`: shared platform components are plausible. No firmware was installed,
and neither cross-model firmware compatibility nor non-Pro HCCAST video was
verified. No result is promoted to `REPRODUCED`.

### Evidence availability at closeout — 2026-09-29

The original temporary USB logs, firmware archive, unpacked binaries, PDF,
camera-helper captures, and detailed inspection report were no longer present.
The findings above were reconstructed from this session's recorded messages and
tool results. Their original integrity checks were not repeated at closeout.
A user-supplied Photo Booth image was still available and retained privately.

The historical firmware hashes remain recorded for future identity comparison,
but a hash alone does not recover the missing file. Raw evidence, vendor files,
and the detailed local handoff remain outside the publication candidate.

### Static reacquisition and app analysis — 2026-09-29

After the closeout above, the user resumed the app/firmware investigation.
`OBSERVED`: official English and Chinese Pro archives contain the same firmware
member, matching the recorded historical SHA-256. Fresh outer/inner container
and uImage CRC checks pass; the extracted application also matches its historical
hash. The official V2 and Pro manual QR codes both identify HCCast 3.2.2.

`OBSERVED`: a manufacturer-linked HCCast 3.3.0 APK uses the SETV URL to fetch
update JSON, selects `FW_URL`, and sends firmware via UPGI then UPG. Its upload
loop was checked against DEX instructions. This is static client evidence;
no Qshot SETV, video session, updater acknowledgement, or flash was produced.

`OBSERVED`: HCLink 1.3.5 and 7RYMS 0.0.3 were subsequently acquired and analyzed.
HCLink's invoked service retains HCCAST framing, sends one-second PINGs, and
uploads firmware in 1,024-byte payloads; upload and heartbeat were checked against
DEX. Its network path uses control TCP 8980 and local video/audio listeners
8981/8982. The 7RYMS Qshot UI selects bundled remote firmware `125_brc_v1.1.6.ufw`
and enables JieLi device authentication over BLE service `0xae00`, write `0xae01`,
notify `0xae02`. All 24 acquired APKs (bases and splits) passed signature
verification; split signers match their respective bases. This establishes app
bytes and client behavior, without exercising either hardware updater.

`OBSERVED`: a further DEX cross-check maps 7RYMS command framing (`fe dc ba`,
flags/opcode, BE u16 body length, sequence/status and `ef`). The focused record
includes explicitly synthetic block-request/response examples. Jetson SSH and
the installed identification command were checked without USB access;
`UNIT-TESTED`: 70 local probe/transport tests pass. The proposed physical check
uses the existing command's 500 ms response limit.

After explicit approval and connection confirmation, the Jetson exposed
`1cbe:0005`, interface 0 `ff/06/50`, with bulk endpoints OUT `0x02` / IN `0x81`
and 512-byte packets. `OBSERVED`: saved binary descriptors confirm these fields.
The device-node access precheck failed and the node subsequently disappeared;
no interface was claimed and **zero SETR packets were attempted**. A permission
denial is not established because the node was disappearing. Kernel logs show
52 disconnects and repeated descriptor/address errors `-71` in a 3-minute,
40-second interval beginning before the probe. Connection stability and the
physical cable/port/power arrangement need investigation before another request.

After the user added external power while retaining C-to-C, `OBSERVED`: no known
monitor identity appeared in 61 passive samples over 30 seconds. Subsequent
kernel reads showed Jetson USB `device` role with stock NVIDIA `l4t` (`0955:7020`)
bound, controller state `default`, and unknown speed. SSH remained on Wi-Fi.
This is role/configuration evidence, not successful enumeration by the screen.
No SETR was attempted. The user subsequently authorized a temporary direct
`18d1:2d00` gadget identification test with stock-profile restoration. Its sudo
preflight required a password and stopped before any gadget change. The user
then entered it locally and ran the test. `OBSERVED`: the direct Android gadget
was created and bound, but the 10-second wait received `BIND` without `ENABLE`.
Zero SETR requests were attempted. The saved `NO_SETV` outcome is a USB
configuration timeout, not a failed HCCAST response. Independent comparison of
before/after snapshots confirmed stock identity, strings, function/configuration
links and UDC restoration; the test gadget and FunctionFS mount were gone.
A subsequent live read confirmed stock `l4t` remained bound. Exact Qshot-side
socket use and screen UI remain the next physical details to establish.

The user then changed to USB-A, removed external power and requested a retry.
`OBSERVED`: one configured `05ac:12ad` stayed present in 13 samples over six
seconds, interface 0 `ff/2a/ff`, bulk OUT `0x01` / IN `0x81`, 512-byte packets.
Its existing device node denied write access to the SSH user. `sudo -n` stopped
for password entry before the host probe ran; zero SETR requests were sent.
The staged one-request host check initially awaited interactive password entry.
Its transport/probe software suite passed 70 tests.

`HARDWARE-VERIFIED`, 09:47:12 UTC: the user then ran that check with sudo. One
20-byte SETR received a valid 332-byte SETV frame (316-byte payload), product
`HC15B100`, version `2511261024`, with no read/write/parse errors and exit code 0.
Independent local parsing confirmed the saved raw frame and decoded fields.
The process exited, and the same configured USB address remained present. The
advertised metadata endpoint is a private IPv4 address and was not contacted.
This establishes HCCAST identification through USB-A; no video, audio, settings
or firmware command was sent. Raw response hash:
`31748a1562ade46bda42656b3b09f3b3b19fe47966564a2cffb43e9bcbd862ee`.

`HARDWARE-VERIFIED`, 11:00:48–49 UTC: the separately approved static-image
checkpoint received a fresh 332-byte SETV (sequence 1, same product/version),
then sent one 36-byte landscape SINF and one 472,632-byte VID containing the
selected 472,616-byte H.264 access unit. The encoder dimensions were 1280x720.
All three application writes returned, no STOP was received during the one-second
observation, the parser had no discarded or pending bytes, and the runner closed
USB and exited 0. A later read found no checkpoint process and the same configured
USB address. The user's supplied photograph shows the matching goblin/sorceress
scene with both HUD corners and score/stage visible. Raw logs and the photograph
are preserved privately; copied hashes and outgoing-message hashes were checked.
The original machine result awaits visual confirmation because the runner cannot
observe pixels; the separate photo supplies that confirmation.

This verifies one static frame on this pairing. It does not establish motion,
sustained streaming, latency, native panel resolution or reconnect behavior.
The completed one-use marker remains intact; no hardware test was repeated when
reviewing the photograph. Raw response SHA-256:
`aab9aba9b980bcba1d28a133260031eceefe0a9c1db1adac48c3ade9dc42991a`.

`OBSERVED`, 11:32:10–16 UTC: the subsequent motion checkpoint sent 50 video
access units over 4.905 seconds after fresh SETV and landscape SINF. All 52
application-message writes returned; their hashes match locally reconstructed
SETR/SINF/VID messages. Local decoding produces 50 distinct frames without
errors. The only received message was valid SETV (sequence 2); no STOP, discarded
or pending parser bytes were recorded. USB close returned, exit code was 0,
and the runner process was absent afterward.

The user reported a static image with no movement and confirmed no rotation or
scale change. Their photo shows a cropped section of the pattern with no visible
counter. This is not a successful motion result. The cause remains unresolved;
software decoding and completed writes do not establish receiver presentation.
The follow-up used three solid colors and independent frames to make changes
visible despite cropping.

`OBSERVED`, 11:46:26–30 UTC: that follow-up sent 15 independent video frames
over 2.802 seconds, with valid SETV (sequence 3), all 17 outgoing-message hashes
verified, exit 0, USB close returned and no STOP. The user reported multiple
visual states. This supports visible image updates; exact color names are not
used to infer a renderer defect. Smooth motion, cropping and color fidelity remain
unverified by that color check.

`HARDWARE-VERIFIED`, 12:03:41–47 UTC: the subsequent monochrome test sent
25 independent IDR frames at 5 fps over 4.804 seconds after one SETR and one
landscape SINF. All 27 outgoing-message hashes match reconstructed bytes; the
332-byte response independently parses as SETV sequence 4 with the same
product/version. No STOP or parser residue was recorded, USB closed and the
runner exited 0. A later read found no runner process and the same USB address.
The user confirmed that the counter advanced from 00 to 24 and the white square
moved as described. Their photo shows final 24 with the marker back at the left.
The user report confirms motion; the still photo corroborates the final frame.
Copied logs, media and photo are retained privately, with verified hashes.

This establishes bounded independent-frame motion on this pairing. The photo
shows an enlarged, horizontally stretched graphic compared with the encoded
preview. Exact crop/scaling, higher rates, sustained playback and the earlier
predictive-frame result remain unresolved. Original machine evidence and the
completed marker are preserved. Raw response SHA-256:
`0a7dab2a61076d67316f424265d8194604204f340b0c4e0ac7a9acdf1354d205`.

`HARDWARE-VERIFIED`, 12:22:42–48 UTC: the geometry checkpoint changed only
SINF from `(1, 1280, 720, 720, 1280)` to `(1, 1280, 720, 1280, 720)`.
The same 25 IDR frames at 5 fps transferred over 4.803 seconds. All 27 message
hashes were verified; SETR and all VID bytes match the previous run. SETV
sequence 5 identifies the same product/version. No STOP or parser residue was
recorded; USB closed, exit was 0, and the process was absent afterward with the
same USB address present. The user explicitly confirmed the five-second
animation, a square white block and numbers without horizontal stretching.
This verifies the source-dimension correction for this test on this pairing.
Quantitative calibration, full-frame crop measurement, higher frame rates and
sustained playback remain unverified. The CLI now applies the same orientation
rule to source dimensions. Original evidence is preserved privately. Raw response:
`295b6e11d5b8fd90581fd4030c6135460e83b455a7920cce1fcb9f83711e2922`.

`HARDWARE-VERIFIED`, 13:59:15–21 UTC: the user-requested 10 fps rerun sent
50 independent IDR frames over 4.903 seconds with the corrected landscape SINF.
All 52 outgoing-message hashes match reconstructed bytes. The 332-byte response
independently parses as SETV sequence 7 with the same product and version.
No STOP or parser residue was recorded; USB closed and the runner exited 0.
The user confirmed that the animation progressed smoothly, without stops or
catches, and completed. This establishes bounded five-second playback at a
10 fps sending rate on this pairing. Display cadence was not instrumented;
rates above 10 fps, sustained playback and predictive-frame playback remain
unverified. Original logs and the user observation are retained separately in
private evidence. Raw response SHA-256:
`703fd0ff1033fd50e926517775596ce5b6ac61985f8fac80045572a8f7129a46`.

`HARDWARE-VERIFIED`, completed at 14:18:30 UTC: the 30-second checkpoint sent
300 independent IDR frames at 10 fps over 29.903 seconds in one connection.
The media repeats the verified five-second clip six times without re-encoding.
All 302 outgoing-message hashes match reconstructed bytes. The 332-byte response
parses as SETV sequence 8 with the same product and version. No STOP or parser
residue was recorded; USB closed and the runner exited 0. The user reported
six smooth passes with no issues. This establishes 30-second playback on this
pairing; longer runs, higher rates and predictive-frame playback remain unverified.
The original machine record and user report are retained separately in private
evidence. Raw response SHA-256:
`2905560a1d47f2e99fc662fcd6e9b63dc490216a2f895434817bb406124f3cf4`.

`HARDWARE-VERIFIED`, completed at 14:47:46 UTC: the controlled predictive-frame
test sent five IDR frames and 45 P frames at 10 fps over 4.901 seconds. It used
the same source pattern and corrected landscape SINF as the successful
independent-frame test. Video payload was 50,575 bytes, about 80% less than that
baseline. All 52 outgoing-message hashes match reconstructed bytes. The saved
332-byte response parses as SETV sequence 9 with the same product and version.
No STOP or parser residue was recorded; USB closed and the runner exited 0.
The user reported smooth playback. This verifies predictive-frame playback for
this five-second configuration. Longer predictive runs remain untested, and the
cause of the initial static report remains unresolved. Original machine evidence
and the later user report are preserved separately. Raw response SHA-256:
`83a339b452428288c4e1c0f56247e139941711d0ca7694bba1c47f98e28c4018`.

`HARDWARE-VERIFIED`, completed at 15:17:03 UTC: the five-second 30 fps test sent
150 frames (five IDR and 145 P) over 4.967 seconds, retaining corrected landscape
SINF. Video payload was 65,526 bytes; all 152 outgoing-message hashes match the
prepared sequence. The saved 332-byte response parses as SETV sequence 10 with
the same product and version. No STOP or parser residue was recorded; USB closed
and the runner exited 0. The user reported smooth playback. This verifies the
five-second configuration at a 30 fps sending rate on this Qshot/Jetson pair;
display cadence was not instrumented. Longer predictive runs remain untested.
Original machine evidence and the user report are retained separately. Raw
response SHA-256:
`3a458f504ea1085b0dae929b9020cfa0ed82eefdc16cc937991d1215ef19178e`.

`HARDWARE-VERIFIED`, completed at 15:45:17 UTC: an isolated 1280x720 desktop
with Chromium playing a public video sent 1,800 frames over 61.676 seconds.
The target was 30 fps; average transmission was 29.18 fps. All 1,802 outgoing
message hashes match the recorded video and framing; SETV sequence 11 identified
the same unit. USB closed, exit was 0 and all source processes stopped. The user
initially reported a frozen image, then clarified that playback was fine before
the timed stop. This verifies bounded live desktop/video playback on the Qshot
and Jetson USB-A pair. The final source image shows counter 61 and clock 08:45:17.
Display cadence and end-to-end latency were not instrumented. Longer runs remain
untested. Original machine evidence and both user reports are retained privately.

The [focused investigation record](FIRMWARE_AND_APP_REVERSE_ENGINEERING.md)
contains hashes, source call sites, firmware layout, current package-migration
leads, acquisition gaps, and the completed physical checkpoints. Vendor artifacts
and local cache locations remain private. This restores binary evidence, not the
missing historical USB captures or proof of non-Pro compatibility.


`HARDWARE-VERIFIED`, completed at 20:32:43 UTC: the Mac-controlled 1280x720
Qshot desktop session sent 9,000 live frames over 306.586 seconds, then 60 ending
frames. Target rate was 30 fps; average transmission was 29.36 fps. All 9,062
outgoing hashes match the independently reconstructed messages, and all 9,060
frames decode without errors. SETV sequence 14 identifies the same unit. No STOP
or parser residue was recorded; USB closed, exit was 0, source processes stopped,
and preview listeners and the temporary X socket closed. The user reported,
"It completed, no issues." This verifies the bounded Mac-control/Qshot session;
YouTube-specific playback details, manual Stop/Ctrl+C and longer sessions remain
unverified. Original evidence and user confirmation remain separate and private.

## Protocol fixture validation

A generated one-second, 1280x720, 5 fps Annex-B H.264 fixture produced:

```text
63 NAL units
5 access units
largest access units around 95 KiB
```

This supports two implementation requirements:

1. Annex-B start codes remain part of the encoded stream.
2. Logical HCCAST `VID` packets may exceed 64 KiB and are fragmented only at the
   USB transfer layer.

Fixture tests are `UNIT-TESTED`, not physical interoperability evidence.

## Raspberry Pi gadget validation gate

This gate applies to the separate RK-X40F gadget route. Qshot host-USB
validation is recorded below. Raspberry Pi gadget output becomes `HARDWARE-VERIFIED` only after one named Pi model/image:

1. exposes a fresh usable UDC;
2. enumerates as direct `18d1:2d00`;
3. receives FunctionFS endpoint configuration;
4. parses a valid `SETV`;
5. produces visible output from the known-good fixture;
6. cleans up and returns to its defined stopped state.

The later live virtual-surface test is a separate milestone.

## Current unknowns

- Raspberry Pi live sessions longer than five minutes and RK-X40F gadget protocol/rendering parity.
- Compatibility with a second RK-X40F-family unit or revision.
- R36S gadget feasibility.
- Audio framing and playback.
- Automatic reconnect and power-cycle recovery.
- Reboot persistence.
- Long unattended thermal and stability behavior.
- Any pixel-addressable framebuffer transport.

Bounded Qshot host-USB playback is `REPRODUCED` across Jetson and Raspberry Pi 5.
No second physical screen unit has been tested.

## Raspberry Pi 5 Qshot playback — 2026-10-01

`HARDWARE-VERIFIED` on Pi 5 Model B Rev 1.1, Debian 13 aarch64, kernel
`6.18.34+rpt-rpi-2712`. `REPRODUCED` from the Jetson for this bounded fixture
on the same Qshot V2 non-Pro; this is not another-unit compatibility evidence.

- USB host path: `05ac:12ad`, interface 0 `ff/2a/ff`, bulk OUT `0x01` / IN `0x81`.
- SETV: `HC15B100`, version `2511261024`; unchanged from the Jetson unit.
- Sequence: one SETR, landscape SINF `(1, 1280, 720, 1280, 720)`, 150 VID frames.
- Clip: five keyframes plus 145 predictive frames, 1280x720, 30 fps sending rate.
- Transfer: 65,526 payload bytes, 67,926 video wire bytes, 4.966929 seconds.
- Independently verified all 152 outbound hashes against the prepared sequence.
- Playback response: valid 332-byte SETV, zero parser discarded/buffered bytes.
- No STOP or reported error; USB close returned and the process exited 0.
- User confirmation: "Yes, it works correctly, and the proportions are correct."

The earlier standalone identity request received 348 bytes: a valid 332-byte
SETV plus 16 unparsed prefix bytes, preserved in private evidence. The playback
run above had no such prefix. Original machine records remain unchanged; user
confirmation and independent verification are separate evidence records.

An encoder-only test also produced and decoded 150 1280x720 frames in 5.011388
seconds. SPS timing specifies 30 fps; raw-stream ffprobe rate estimates were
inconsistent. This synthetic source does not verify live desktop capture or
end-to-end latency. Panel refresh cadence was not instrumented.

The prepared runner passed 109 relevant software tests. Its only functional
source-pin change accepted a reviewed missing-PyUSB message update; USB transfer
code was identical to the Jetson reference. The later live checkpoint is recorded
below; reboot recovery and unattended operation remain unverified.


## Raspberry Pi 5 live desktop and video — 2026-10-01

`HARDWARE-VERIFIED`: same Pi 5 and Qshot HC15B100/2511261024, isolated
1280x720 Xvfb/Openbox/Chromium desktop, software x264, 30 fps target.
The desktop showed a clock, counter, moving white square and looping public
sample video. The existing Waveshare/Wayland configuration was preserved.

- First run: 1,800 live frames in 60.026826 seconds, then 60 ending frames.
  The user missed this run; it supplies transport evidence only.
- User-requested replay: 1,800 live frames in 60.044873 seconds, then 60 ending
  frames. Completed 14:16:40 UTC; user: "Yes, it ran correctly."
- Both runs: all 1,862 outbound message hashes matched; all 1,860 captured
  access units decoded without errors. No STOP or parser residue; USB closed,
  exit 0, and temporary desktop processes and X sockets were gone.
- The browser reported continuing video playback in both sessions.
- Preparation: 119 relevant software tests passed. A 450-frame desktop capture
  completed in 15.051602 seconds and decoded without errors before USB testing.
- An initial launch stopped before USB because `sudo -v` expected a terminal.
  Existing Pi passwordless sudo permission was verified; the precheck changed to
  `sudo -n true`, with regression coverage. No sudo policy was changed.

Raw captures, per-run records, and separate user confirmation remain private.
The sending rate is measured; panel refresh cadence and end-to-end latency were
not instrumented. The later Mac-control checkpoint is recorded below; audio,
reconnect/reboot recovery and persistent service behavior remain unverified.


## Raspberry Pi 5 Mac-controlled desktop — 2026-10-01

`HARDWARE-VERIFIED`, completed 14:28:12 UTC on the same Pi/Qshot pairing:
9,000 live frames at 1280x720 in 302.475735 seconds, targeting 30 fps and
averaging 29.75 fps in transmission, followed by 60 ending-card frames.
The user confirmed: "Yes—Mac control and the Qshot both worked correctly."
The question explicitly covered smooth output through the ending card.

All 9,062 outbound hashes matched independently reconstructed messages; all
9,060 captured access units decoded without errors. No STOP or parser residue
was recorded; USB closed, the worker exited 0, all recorded source processes
stopped, and Pi preview listeners and the temporary X socket closed. The
remaining Mac SSH tunnel was explicitly terminated after verifying its command;
its local listener was then absent. That SSH exit 255 reflects final tunnel
cleanup after the remote session had already reported success.

Preparation installed x11vnc 0.9.17-1, noVNC 1:1.6.0-2 and websockify
0.12.0+dfsg1-4+b1 with dependencies: 13 new packages, no upgrades/removals or
service restart. The software-only remote-control check encoded for 60.024875
seconds, recorded one keyboard event and visible sample-video playback, and
closed its processes. Live control used loopback-only ports through SSH.

The user initially could not access the Codex browser panel. A regular Chrome
tab exposed the session. Chrome reported a noVNC module-export error at the
previously used loopback address; a fresh localhost origin connected correctly.
The visible remote browser subsequently showed YouTube navigation. The landing
page's video metric was false in this run because it does not track other pages;
specific YouTube content or playback quality was not separately established.

Original machine evidence and later user confirmation remain separate/private.
No firmware, native Waveshare display configuration, boot service, audio or
reconnection behavior was changed. Longer Pi runs remain unverified.


## User-operated Mac launcher — 2026-10-01

`HARDWARE-VERIFIED`: the user ran the Mac Start launcher and reported,
"ok, it ran great." Its saved Mac session log reports SESSION_ENDED at
14:45:43 UTC with source_only=false, mac_control=true, USB return code 0,
continuing browser video and the temporary desktop/X socket closed.
Original machine output and later user confirmation remain separate/private.
This confirms the launcher's end-to-end physical path. Per-frame hashes and
current remote process state were not independently rechecked for this run;
manual early Stop remains software-tested only.
