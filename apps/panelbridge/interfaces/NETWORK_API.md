# Network helper API v1

Implementation contract for the approved PB-1 helper. This is an internal build
note, not a claim that the service is installed or hardware-verified.

System D-Bus name/interface: `org.panelbridge.Helper1`.
Object: `/org/panelbridge/Helper1`. Payloads are bounded JSON strings with
`api_version: 1`. Errors use the same D-Bus prefix and do not include secrets.

| Method | Arguments | Result |
|---|---|---|
| Status | none | State, owner-present boolean, recovery availability, last error, restoration stage errors |
| Discover | none | Candidates: opaque id, display name, manufacturer, transport, WFD availability and private local peer_address for durable selection |
| Connect | candidate id | Selected peer name/address/IP, P2P interface, active connection UUID, lease deadline |
| KeepAlive | none | Updated lease deadline |
| Release | none | Network restored boolean and conflict/error list |

Only the root-enrolled normal account with an active local graphical session can
start interactive operations. The enrolled account can still read Status and
Release its own lease when its session locks or exits. Release cannot affect a
different unique D-Bus owner's lease. A separately installed rescue identity can later
use these same operations only from its fixed system service while active;
Release also remains allowed while that exact service is deactivating. Do not implement
a caller-supplied rescue flag. Root integration testing uses an explicit private
test harness; production authorization must not have a test bypass.

One D-Bus unique sender owns discovery and streaming until Release, disconnect,
or lease expiry. Return Busy to another mutating caller. Status is read-only and
restricted to enrolled identities. Candidate ids expire with the lease; Connect
must resolve the current peer and recheck its family and WFD capabilities.
Candidates are discovered by `EBPS[I1]-[A-Za-z0-9]+`, never one unit's exact name.
The controller retains the user's selected unit privately and must not substitute
another sole candidate when that binding is missing.

Require connected Ethernet recovery before leaving a home Wi-Fi association in
v1. Keep the existing default route. Save connection UUID, device autoconnect,
and initial connection state without reading secrets. Use an NM checkpoint and
volatile app-owned P2P connection, with automatic cleanup on helper disconnect.
Do not delete shared profiles. Restore the recorded values, not guessed defaults.

Discover has a 20-second radio budget. Connect has a 45-second budget. D-Bus and
subprocess timeouts are capped by each remaining operation budget. A 60-second
owner lease is renewed by KeepAlive; the NM checkpoint has an independent
120-second rollback window. Fail closed if extending the checkpoint fails.
Do not hold the main event loop during blocking operations. Serialize network
changes and teardown with cancellation; never let an expired operation reactivate
the link after cleanup. Three failed connection attempts require explicit retry
from the controller and last-known-good settings. Status then reports
`retry_required: true`; Discover returns RetryRequired until an explicit Release
rearms the helper. A successful Connect clears the failure count. Failed Connect
already restores the network; the controller must not automatically Release
after each failure, since Release represents the user's explicit retry.

Address selection supports both P2P roles. As client use the assigned gateway.
As group owner require exactly one associated station matched to a current DHCP
lease on the selected group interface. In both cases validate the subnet, local
address inequality and route interface. No subnet scan or guessed receiver IP.

Root handles NM and bounded interface/lease inspection only. It must never launch
the capture, GTK or media worker. Those run as normal-user processes and adopt
the returned connection. No caller-supplied commands, paths, units, interfaces,
package names, URLs or arbitrary NM dictionaries cross this API.

Enrollment is a root-owned regular file at `/etc/panelbridge/enrollment.json`.
The installer, not a D-Bus method, sets its UID bindings. Source/helper code and
policy are root-owned at install. Reject symlinks and writable enrollment files.
Journal app network ownership under `/var/lib/panelbridge`; preserve records of
incomplete restoration and never claim success if a cleanup stage failed.

`Status.restoration_errors` is an additive list of fixed stage identifiers, empty
unless state is `restore_required`. It excludes raw exception text, addresses,
paths and connection identifiers. A successful restoration clears the previous
`RestoreRequired` error. Final verification may wait for recorded Ethernet routes
within the existing cleanup deadline; it never repeats mutations or accepts a
foreign Wi-Fi/P2P connection. The session supervisor waits up to 60 seconds for
Ethernet recovery before automatic connection, without spending radio retries.
