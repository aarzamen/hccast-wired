# Rescue runtime interface

IMPLEMENTED source and UNIT-TESTED software lifecycle. This interface makes no
claim of visible panel output, desktop-independent pairing, or native panel
resolution. Those remain separate hardware checks.

## Integration

```python
from panelbridge.rescue import RescueRuntime

runtime = RescueRuntime(
    network_client,
    receiver_address=saved_receiver_address,
    expected_uid=installed_rescue_uid,
    health=sample_health_async,
)
result = await runtime.run(seconds=600)
```

The system supervisor runs this code under the fixed dedicated nonroot service
UID. The runtime checks both real and effective UID; it never elevates or changes
identity. The integrator supplies the root-owned saved binding and the enrolled
helper client. No interactive session, Wayland output, portal, framebuffer, input
device, shell, or desktop process is used. The worker receives a minimal locale
and PATH environment, without inherited desktop or user-bus variables.

`run()` is one-use, with a finite duration greater than zero and at most 14,400
seconds (default 600). `request_stop()` requests an orderly stop; cancelling the
task also closes and joins owned resources. Repeated cancellation waits for the
same cleanup. A new instance and explicit supervisor decision are needed for a
new run; do not configure an unbounded restart loop around three failed attempts.

The default worker is the installed `panelbridge-wfd-worker` in the application
library directory. The optional `worker` argument requires an absolute path and
is trusted deployment configuration, never a recovery-control argument.
`RescueTimeouts` permits shorter positive deadlines, capped at the defaults.

## Injected contracts

The async network adapter exposes the existing Network API v1 methods:

- `discover()` returns at most 64 current candidates, including their private
  `peer_address`, opaque `id`, and `wfd_available` flag.
- `connect(candidate_id)` returns the existing exact adoption tuple: interface,
  peer name/address/IPv4, connection UUID and lease deadline.
- `keep_alive()` returns API v1 and a monotonic lease deadline with enough time
  left for the next heartbeat and its deadline.
- `release()` returns `network_restored: true` only after completed restoration.
- `close()` disconnects its single unique helper owner and joins any pending
  operations, including work whose original caller was cancelled.

All methods must support cancellation. They must serialize late network work
with release/disconnect so an abandoned connection cannot reactivate later.
The existing `NetworkClient` owns the private bus connection; root integration
is responsible for its dedicated service authorization and supervisor policy.
Closing the owner is mandatory even if explicit restoration fails or times out.
The runtime reports that failure instead of claiming restoration succeeded.

`health` is an async callable returning `temperature_c` and `throttled_bits` in
the existing HealthMonitor shape. CPU data may also be present but is not a
substitute for either safety measurement. The sampler must be bounded and
cancellation-responsive, and must own/join any subprocesses it uses. The runtime
samples before discovery, then every second with a three-second sample deadline.

Unknown/nonfinite/out-of-range health, temperature at or above 70°C, any current
throttle/undervoltage/frequency-cap/soft-temperature flag, new sticky fault flags,
or unknown flag bits stop the run. Existing sticky history is recorded as the
initial comparison baseline and does not alone imply a new active fault. A failed
health read does not trigger another connection attempt.

## Media and lifecycle

The default Cairo renderer emits exactly 1280×720 opaque BGRx bytes on a
little-endian host. This is the generated source size and requested 720p30 wire
mode, not a native-panel-resolution claim. The fixed profile uses five generated
frames per second, 30 requested wire frames per second and 4096 kb/s. Lower source
rate is neither a measured power saving nor a lower wire refresh claim. The
existing worker validates receiver negotiation; the runtime does not add modes.

The high-contrast monochrome card contains a seconds counter, alternating square,
fixed connection status, and instructions to retain power/Ethernet and use an
already authorized recovery connection. The card contains no receiver identity,
network names, credentials, arbitrary logs, or claimed input controls. Cairo
caches only the current frame. The internal renderer seam is synchronous and
must return one correctly sized `bytes` frame; production uses `CairoCard`.

The runtime passes the same raw-FD and adoption arguments used by `DesktopMedia`,
with `--continuous`. It generates no media subprocess other than the worker.
`ProcessGroup` starts that worker in an owned process group. Pipe writes use
nonblocking bounded chunks and cancellation-aware writable waits. Only one frame
is in flight; a slow or missing consumer cannot accumulate frames indefinitely.

There are at most three connection/stream attempts, with two- and four-second
backoff. Selection requires one exact saved peer address; another sole receiver
is never substituted. The returned link address is checked again before spawning
the worker. Failed helper Connect already restores its transaction, so the runtime
does not call Release between those failed Connect attempts. A connected attempt
is released after its worker is stopped and before another discovery attempt.

Lease renewal occurs immediately before and after connection setup, then every
15 seconds with an eight-second deadline. This avoids spending most of a
60-second lease in the 45-second connection operation and then waiting another
15 seconds. Lease renewal, health sampling and raw pipe writes are independent
tasks. A stalled heartbeat cannot block a health-triggered stop.

Worker STREAMING has a 40-second deadline. Once streaming, absence of increasing
`pipeline-telemetry.encoded_buffers` for 12 seconds fails the attempt. That is
sender progress only, not receiver delivery. EOF, worker exit, malformed oversized
event output, broken pipe, or write timeout also ends the attempt. Stderr is
drained in bounded chunks and is not retained or rendered.

Every exit cancels/joins producer and reader tasks, closes the owned pipe, signals
and reaps the owned process group (three-second TERM then three-second KILL wait),
requests network restoration, and closes the helper owner. Cleanup has finite
network deadlines. An unreaped child remains tracked by ProcessGroup and causes
an error; the runtime does not claim a successful stop in that case.

Successful `run()` returns `status`, `reason` (`requested` or `duration_limit`),
`attempts`, `frames_written`, and `network_restored`. A completed pipe write is not
physical delivery. Failures raise `RescueError` with a fixed diagnostic code;
restoration/owner-close failures remain visible. Authenticated control, guardian
activation, system-service installation and deployment are integrator work.

## Focused verification

`tests/test_rescue.py` uses synthetic network responses and real tiny subprocesses
and pipes. It covers exact selection and returned binding checks, partial writes,
blocked writes, TERM/KILL cleanup, connection/startup/stream/lease failures,
three-attempt limits, health faults, repeated cancellation, restoration failures,
minimal worker environment and arguments, and invalid frame lengths.

The Cairo frame test requires pycairo. On a host without it this test is explicitly
skipped; lifecycle tests use a known byte frame and do not substitute for Cairo
render verification. On the Pi, run this same focused file with distro Python,
pycairo and pytest, then inspect the card on the actual receiver in the separate
authorized desktop-independent rescue checkpoint.
