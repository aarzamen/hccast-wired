DRAFT

# Walkthrough map

This page is a map for walking through the repository as it stood on
2026-11-05. It follows the decided scope in [docs/SCOPE.md](docs/SCOPE.md): the
Raspberry Pi 500 keeps its normal HDMI desktop and gains one more screen, a
LUODIER/EBPSI reached over Wi-Fi Direct, while Ethernet stays plugged in for SSH
recovery. Read the stops in order. Each stop says what the code does, why it is
there, and what breaks if it is changed carelessly. Paths are relative to the
checkout; the named functions are the places to jump to.

Two unrelated things live here. The **legacy HCCAST Wired driver** under
`src/hccast_wired/` pushes H.264 over USB to Qshot and RK-X40F screens, is
MIT-licensed, and plays no part in the wireless screen. **PanelBridge** under
`apps/panelbridge/` is the wireless path for the LUODIER/EBPSI and is
GPL-3.0-or-later. The older Pi 5 results, the Jetson reference and the
standalone boot goal are history in `docs/`, not current acceptance.

Safe to ignore while walking: `uv.lock`; the icon PNGs under
`apps/panelbridge/assets/`; the pinned upstream archive
`apps/panelbridge/vendor/gnome-network-displays-0.99.0.tar.gz`; the one-line
launchers `apps/panelbridge/packaging/*-launch.py`; the systemd, D-Bus and
`.desktop` boilerplate next to them; and the generated `SOURCE_MANIFEST.json`,
`UPSTREAM_PIN.json` and receipts inside built bundles. They are vendored,
generated or boilerplate and hold no behavior of their own.

## Stop 1: the front doors

Files: `apps/panelbridge/README.md`, `apps/panelbridge/interfaces/NETWORK_API.md`.

**What.** The README states the Pi 500 scope, the one physical demonstration,
the open freeze and timeout, and the fact that no public installer release
exists. NETWORK_API.md is the contract for the only privileged piece: a root
helper on the system bus named `org.panelbridge.Helper1` with five methods
(Status, Discover, Connect, KeepAlive, Release), fixed time budgets, a 60-second
lease and a NetworkManager checkpoint with a 120-second rollback window.

**Why.** These two pages are where the wireless path is separated from the
older USB drivers and from the Pi 5 evidence. The README points at
[docs/PI500.md](docs/PI500.md) for the current result and at
[docs/VALIDATION.md](docs/VALIDATION.md) for the claim ledger.

**Careless change breaks.** Documenting a helper method that `helper/service.py`
does not implement, or folding Pi 5 or Jetson claims into the Pi 500
acceptance, turns the front door into a false map.

## Stop 2: Start, Stop and the states a person sees

Files: `apps/panelbridge/panelbridge/controller.py`,
`apps/panelbridge/panelbridge/session_service.py`,
`apps/panelbridge/panelbridge/startup_choice.py`.

**What.** `Controller.command()` accepts the verbs listed in
`interfaces/SESSION_API.md`. `Controller._stream()` is the whole life of a
session: discover, `_choose()` the saved receiver, `network.connect`,
`media.start()`, `media.ready()`, then status `streaming` until `media.wait()`
returns. A failure records itself in the state store, retries after 2 and then
8 seconds (`retry_delays`), and gives up after three attempts with status
`error`. The states a person sees are `stopped`, `discovering`, `connecting`,
`streaming`, `retrying` and `error`, always with a plain `message`.
`Controller._release()` asks the helper to put the network back and refuses to
carry on if it reports the network was not restored. `tick()` sends a keep-alive
every ten seconds through `_heartbeat()` and reverts an unconfirmed display trial
when its deadline passes. `session_changed()` pauses capture when the desktop
locks and resumes it on unlock.

`SessionService` wraps that controller in a normal-user D-Bus service
(`org.panelbridge.Session1`) and runs `watch_state()` four times a second. It
polls three things: whether the graphical session is active, whether the Pi is
healthy (`HealthSampler`), and whether the helper reports Ethernet recovery.
Automatic startup is decided by `autostart_enabled()` in `startup_choice.py`:
`--no-autostart` always wins, an optional OLED choice of `desktop` or
`headless` suppresses the first connection, and otherwise the service waits up
to `STARTUP_NETWORK_TIMEOUT` (60 seconds) for Ethernet before pressing Start on
the user's behalf. The GTK window in `ui/app.py` is only a client of this
service, so closing the window does not stop the stream.

**Why.** One generation counter and one transition lock make Stop win over any
in-flight Start, and the release rule means a failed session never leaves home
Wi-Fi disconnected without saying so.

**Careless change breaks.** Dropping a `_can_stream(generation)` check lets a
stale task restart capture after Stop. Skipping `_release()` on an error path
leaves the Pi on the Wi-Fi Direct group with no route home. Removing the health
or session gates in `watch_state()` lets capture run on a locked or overheating
Pi. Changing the autostart wait removes the Ethernet-first safety that the
scope requires.

## Stop 3: pixels to radio

Files: `apps/panelbridge/panelbridge/output.py`,
`apps/panelbridge/panelbridge/media.py`, `apps/panelbridge/panelbridge/models.py`,
`apps/panelbridge/native/wfd/`.

**What.** `OutputAdapter.select()` finds exactly one virtual compositor output
(`HEADLESS-n` or `NOOP-n`) through `wlr-randr --json`. `OutputAdapter.apply()`
turns it on at the profile's size, 30 Hz and scale, and places it to the right
of every physical output using `_right_edge()`, which is why the HDMI desktop
keeps its own space. `OutputAdapter.capture_arguments()` builds the
`wf-recorder` command that writes raw `bgr0` frames at `content_fps` into a
pipe. `DesktopMedia._start()` applies the output, creates that pipe, spawns the
native worker with `--source raw --fd` plus the link details, then spawns the
capture. `_read_events()` parses the worker's JSON lines; readiness means the
sink reported `ND_SINK_STATE_STREAMING` and the encoder reported real frames.
`_watch_progress()` declares a stall after `stall_seconds` without new encoded
buffers, and `ProcessGroup.close()` terminates and then kills every owned
process group.

The native sender in `native/wfd/` is GNOME Network Displays 0.99.0 plus four
patches and the files `panelbridge-wfd-worker.c` and `pb-wfd-*.c`, assembled by
`build-worker.sh`. In the worker, `raw_need_data()` feeds captured frames into
GStreamer, `choose_peer()` and `adopt_existing()` attach to the connection the
helper already made, `pb-wfd-profile.c` pins the advertised mode, and
`pb_wfd_telemetry_emit()` reports counts.

**Why.** Three different numbers are easy to confuse. The capture rate is
`Profile.content_fps`, what `wf-recorder` is asked for. The negotiated mode is
`wire_width`, `wire_height` and `wire_fps`, what the receiver agreed to in RTSP,
limited to 720p30 or 1080p30 by `Profile.__post_init__()`. Actual visible
playback is what a person sees on the panel, and only an observation or a
recording establishes it. Telemetry counts encoded buffers; it never counts
displayed frames.

**Careless change breaks.** Placing the virtual output anywhere but past the
right edge overlaps the HDMI desktop. Removing `--no-dmabuf` or the raw format
changes what the worker receives. Treating "encoded" as "displayed" produces
claims the ledger forbids. Editing a patch without rebuilding against the pinned
archive breaks `build-worker.sh`, which applies the four patches in order.

## Stop 4: privilege, Ethernet, the model gate and rollback

Files: `apps/panelbridge/helper/network.py`, `apps/panelbridge/helper/service.py`,
`apps/panelbridge/packaging/installer-bootstrap.py`,
`apps/panelbridge/packaging_tools/install_transaction.py`,
`apps/panelbridge/packaging_tools/desktop_setup.py`,
`apps/panelbridge/packaging_tools/maintenance.py`.

**What.** `helper/service.py` is the only root process. `Authority.authorize()`
admits one enrolled normal account with an active graphical session, read from
root-owned `/etc/panelbridge/enrollment.json`, which the installer writes and no
D-Bus call can change. `NetworkHelper.discover()` in `network.py` snapshots the
network, creates a NetworkManager checkpoint, journals it, then scans.
`NetworkHelper.connect()` refuses unless `recovery_available` says Ethernet is
up, extends the checkpoint, journals the connection UUID and activates the
group. `keep_alive()` renews the 60-second lease; `release()` and `_restore()`
put back the recorded values and set state `restore_required` if anything
failed, which the controller surfaces instead of hiding. Three failed connects
require an explicit Release before another try. Root never launches capture,
GTK or media.

`installer-bootstrap.py` is one `.run` file carrying the Debian package, the
matching source archive and the package receipt (`_bundle()`). `main()` runs as
the normal user, checks `_normal_account()`, and hands a hash of itself to sudo;
the small `ROOT_LOADER` copies the file into root-owned staging and re-executes
it with `--root-staged`. `_host()` is the model gate: Linux on aarch64, a
device-tree model of `Raspberry Pi 5 Model B` or `Raspberry Pi 500`, Debian 13
and an arm64 dpkg. `_execute()` journals each phase into numbered result files
through `_save_result()`, simulates the APT plan, checks it with
`apt_plan.inspect_install_plan()`, installs with pinned versions, verifies the
installed files against the receipt, then runs `maintain-launch.py configure`.
Rollback of the five system files is `InstallFileTransaction.apply()` and
`restore()`; the user's own compositor and autostart entries go through
`DesktopSetup.install()` and `uninstall()`, which back up what they replace.
`startup_choice.py` only reads the optional OLED's choice; nothing here writes
to the OLED or its service.

**Why.** The desktop user must be able to connect without a password while root
keeps the radio and the recovery path, and a half-finished install must be
visible and reversible rather than guessed at.

**Careless change breaks.** Loosening `_host()` installs on hardware nobody has
tested. Adding a flag that bypasses `EXPERIMENTAL_INSTALL_ENABLED` removes the
approval gate. Skipping `_native_locks()` or the second plan comparison races
APT. Accepting caller-supplied paths, units or commands across the D-Bus API
hands root to the desktop. Changing restore to invent defaults loses the
owner's Wi-Fi profile.

## Stop 5: notices, packaging and what CI does not prove

Files: `apps/panelbridge/LICENSE`, `apps/panelbridge/THIRD_PARTY_NOTICES.md`,
`apps/panelbridge/packaging_tools/source_bundle.py`,
`apps/panelbridge/packaging_tools/build_package.py`,
`apps/panelbridge/packaging_tools/build_installer.py`,
`.github/workflows/ci.yml`.

**What.** The subtree is GPL-3.0-or-later because the sender is modified GNOME
Network Displays; the root `LICENSE` and `THIRD_PARTY_NOTICES.md` are MIT and
cover only the legacy driver. A change to the sender is traced like this: the
file must be in the `SOURCE_FILES` allowlist, `build_bundle()` hashes it into
`panelbridge-app-source.tar.gz` with a manifest and the upstream pin,
`build_package()` copies that archive into the package at
`usr/share/panelbridge/` beside the worker binary, the license and the notices,
and `build_installer()` refuses unless the package, the receipt and the current
source snapshot agree before embedding them in the `.run` file.

CI runs the Python tests on 3.10, 3.12 and 3.13, Ruff, mypy on the legacy live
controller, a compile pass, and builds the root wheel. That wheel is the MIT
legacy driver only; it does not install PanelBridge. Everything CI checks is
software verification. Pi 500 physical acceptance is the owner's observation,
recorded in `docs/PI500.md` and labelled with the vocabulary in
[AGENTS.md](AGENTS.md).

**Why.** A GPL binary must travel with its exact source, and a green workflow
must never be mistaken for a screen that works.

**Careless change breaks.** Adding a source file without updating
`SOURCE_FILES` ships a binary without matching source. Changing the pinned
archive or recipe hash without reviewing the patches breaks the pin check.
Relicensing upstream code is not possible and the notices say so. Marking CI as
acceptance contradicts the claim ledger.
