# Session controller API v1

Internal implementation contract. System helper operations are separately
specified in NETWORK_API.md. GUI and session controller run as the enrolled user.

Session D-Bus name/interface `org.panelbridge.Session1`, object
`/org/panelbridge/Session1`. `GetState() -> s`, `Command(s verb, s JSON) -> s`.
Signal `StateChanged(s JSON)`. All JSON has `api_version: 1`.
Commands return `{api_version:1, ok:true, result:{...}}` or
`{api_version:1, ok:false, error:"plain text"}`. No shell or arbitrary file API.

State fields:

* `status`: stopped, discovering, connecting, streaming, retrying, rescue, error.
* `message`: short factual text. `receiver`: null or name/transport/verification.
* `candidates`: id/name/manufacturer/transport; private selected binding remains
  in the controller. USB always unavailable in v1 with explanation.
* `profile`: source_width, source_height, content_fps, wire_width, wire_height,
  wire_fps, bitrate_kbps, scale, rotation. Wire resolution is not native LCD size.
* `presets`: recommended/performance/battery_saver. Each has available, reason,
  settings (same profile record when available), measurement (summary or null).
  Uncalibrated presets are unavailable. Never fabricate measured power/latency.
* `health`: temperature_c, cpu_percent, throttled_bits; unknown values are null.
* `trial`: null or kind/seconds_remaining/message. Timed confirmation is visible
  and keyboard accessible. Missing confirmation reverts independently of GUI.
* `features`: list of currently implemented verbs. Disable unsupported controls
  with a reason; never show fake successful actions.
* `feature_reasons`: optional map of verb to a plain-text reason when disabled.
* `capabilities.cpu_caps_mhz`: optional array of currently validated CPU caps.
* `capabilities.trial_clocks_mhz`: optional array of currently permitted trial
  frequencies after the safety gate. Missing/empty arrays disable their controls.
* `recovery`: ssh_available, lan_enrolled, message. No real tokens in state.

Verbs and payloads:

| Verb | Payload |
|---|---|
| discover/start/stop | empty object |
| select | candidate_id |
| apply_profile | complete profile OR preset key (mutually exclusive) |
| confirm_profile/revert_profile/calibrate/restore_defaults | empty object |
| set_cpu_cap | mhz (validated available value) |
| trial_clock | mhz (only after safety gate; no voltage/force_turbo controls) |
| start_recovery_pairing | empty object; UI must never log returned private code |
| about | empty object; returns credits and license records |

GUI pages: Connection, Display, Presets, Advanced, Recovery, About. Native GTK4,
no WebKit. Keyboard/mouse only, readable at 640x360 and 1280x720; scroll when
needed. Keep/Revert and recovery access must remain reachable. Unknown native
resolution, experimental settings and unmeasured power are stated explicitly.
The app's original geometric SVG icon has no vendor mark.

The UI adapter calls this API asynchronously. Failure/disconnection presents a
usable retry page rather than a frozen spinner. A development preview may supply
synthetic state only with a conspicuous 'Preview — no hardware' label; it must
not auto-connect to devices, start a helper, or claim real measurements.

## Optional OLED startup control

The same user's optional OLED panel uses GetState and the fixed start, stop,
confirm_profile and revert_profile commands. It does not run a second sender.
Its status and actions must expire when controller polling stops. A command
acknowledgment means accepted, not that the physical screen is displaying it.

At session startup, root-owned `/etc/oled-panel/device.json` with the exact
boolean `panelbridge: true` enables a boot preference from
`/run/oled-panel/choice.json`. That regular file and its directory must belong to
root or the current desktop user and must not be writable by other accounts.
Its only fields are `panelbridge: true`, `mode` and `boot_id`; the last must match
the kernel's current boot UUID. `wireless` keeps normal autostart. `desktop` or
`headless` suppresses the first automatic connection while leaving manual Start
available. An explicit `--no-autostart` always wins. Missing, legacy, invalid or
stale choices retain normal startup. The session never changes the saved OLED
default or starts/stops the display manager.

The OLED installer checks that its service account matches PanelBridge's enrolled
normal UID. It also requires root-owned `/usr/share/panelbridge/oled-integration.json`
with exactly `{"api_version":1,"startup_choice":1,"session_bus":"org.panelbridge.Session1"}`.
This prevents enabling boot controls against older packages that ignore the choice.
