# PanelBridge worker assignments

Read [AGENTS.md](../AGENTS.md) first. The approved PB-1 plan controls this
wireless-first build. These lanes assign software work within that manifest;
active USB experiments and USB-video implementation remain deferred. Current
assignments and evidence are recorded in the integrator's progress ledger.

The user requested session closeout on 2026-10-02. The lanes below are the
resumption map, not live worker assignments. Optional OLED deployment is deferred.
On resumption, finish the standalone normal-desktop lifecycle and measure frame
rate, efficiency and performance under the approved manifest. Verify the current
installed revision before deploying pending source changes.

## Active PB-1 lanes

The root agent is the integrator and sole hardware owner. Only the integrator
uses SSH, USB, network discovery/pairing, Pi system configuration, physical
streams and boot/recovery tests. Workers have no hardware lease. The integrator
also owns shared schemas, interface decisions, provenance, merges and the final
evidence ledger. Software checks never substitute for the integrator's physical
gates or user observations.

At most two workers run concurrently, with non-overlapping file assignments and
no recursive delegation. The integrator freezes shared interfaces before
parallel implementation and assigns exact files, companion tests and commands
at dispatch. Workers use the provided isolated environment and report missing
dependencies to the integrator for resolution within PB-1; this does not create
a new user approval requirement.

The table follows the approved plan. Its paths are planned ownership scopes,
not claims that each directory, implementation or test already exists. Advance
through the gates in order; later stages do not authorize skipping feasibility.
File ownership can transfer between stages only after the integrator releases
the preceding assignment.

| Stage | Integrator | Worker A ownership | Worker B ownership | Gate |
|---|---|---|---|---|
| 0. Governance and baseline | Activate/reconcile governance, recover Git provenance, preserve baselines; dispatch bounded governance edits | Dependency/source/license manifest; exact source and header audit | Read-only test design mapped to the six DoD rows and available evidence | Scope, license gaps and reversible baseline recorded; no publication |
| 1. Feasibility | Stock labwc/native Screens, capture and P2P experiments; all physical observations and recovery | Output/capture adapters in `apps/panelbridge/native/capture/`; software checks for output selection and source handoff | GND API/profile/rescue adapter in `apps/panelbridge/native/wfd/`; software checks for API handling and bounded input | Adapter checks and supported-target builds pass; integrator proves normal desktop, unattended capture and independent rescue source |
| 2. Core control and recovery | Freeze typed APIs, helper boundaries, state/event records and restore transactions | `apps/panelbridge/panelbridge/controller/`, plus assigned devices, telemetry and calibration modules | `apps/panelbridge/helper/` and assigned recovery units under `apps/panelbridge/packaging/` | Focused state-machine, reconnect, caller-validation and transaction-rollback checks pass; physical fault checks stay with integrator |
| 3. UI and installation | Integrate components; conduct Pi install/upgrade/uninstall and fresh-image gates | `apps/panelbridge/panelbridge/ui/` and assigned assets | `apps/panelbridge/packaging/` and offline bundler | Setup/advanced/recovery flows work; package lifecycle and offline closure checked; integrator verifies privilege prompts and restoration |
| 4. Acceptance and documentation | Physical fault/reboot/power tests, evidence review and final claim labels | `apps/panelbridge/tests/`, with exact files assigned to avoid concurrent test ownership | `apps/panelbridge/docs/`, assigned README/notices and icon refinements | Six DoD rows carry evidence or explicit missing/experimental status; only completed physical gates support completion |

### Acceptance and handoff

Workers report changed files, exact commands, exit results, output and unresolved
gates. Use checks appropriate to each deliverable: source/header review for a
license manifest, requirement-to-test mapping for test design, compiler and API
checks for native adapters, deterministic tests for controllers/helpers, and
package-content/lifecycle checks for packaging. The integrator runs checks that
require the supported Pi, services, privileges, radio or physical output.

Do not invent passing commands or placeholder app tests. A worker adds focused
checks with retained behavior and reports the real build/test entry points.
Planned components without an implementation or runnable check remain unverified.

### Existing Python checks

These commands reference files present on 2026-10-01 and run from the checkout
root in its existing isolated environment. They cover governance and the current
wireless worker's software checks only; they do not validate other planned lanes,
compile the native adapter or prove physical streaming.

```bash
.venv/bin/python -B -m pytest -p no:cacheprovider -o addopts= -q tests/test_public_repository.py
.venv/bin/python -B -m pytest -p no:cacheprovider -o addopts= -q apps/panelbridge/tests/test_wfd_worker.py
```

## Optional legacy briefs

Retained on 2026-10-01 from the earlier HCCAST task queue. These are optional
software-only maintenance briefs, separate from the active PB-1 lanes. The
integrator may dispatch one only when it supports the approved work; none
restarts USB protocol investigation or implements EBPSI USB video.

The restrictions below apply to each legacy worker assignment. They do not
revoke the integrator's approved environment setup, remote/hardware work or local
Git authority. Missing setup is referred to the integrator within PB-1. Workers
remain subject to the sole hardware owner and no-delegation rules above.

### Task 1: Protocol-frame regression coverage

#### Scope

Add deterministic unit coverage for one documented protocol-frame edge case and
make only the smallest source correction proven necessary by that test.

#### Owned files

`tests/test_protocol.py` and, only if the test proves it necessary,
`src/hccast_wired/protocol.py`.

#### Forbidden actions

Within this worker assignment, do not use network or remote services; access
hardware; install or synchronize packages; initialize Git or create repositories; push or publish; or change
unrelated files.

#### Prerequisites

Use the isolated development environment provisioned by the integrator with the
project's `dev` extra. If unavailable, report the missing dependency or environment
to the integrator; environment preparation is authorized within PB-1.
Read `AGENTS.md`, `src/hccast_wired/protocol.py`, and
`tests/test_protocol.py` before editing.

#### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_protocol.py`

#### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the final `UNIT-TESTED` claim.

#### Copy-ready brief

```text
Worker scope: this is an assigned software-only legacy task under AGENTS.md.
Use the integrator-provided isolated environment with the project dev extra. Report
missing dependencies to the integrator for resolution within PB-1. Do not use SSH,
network discovery or physical streams, change Pi configuration, or delegate work.

Write a failing deterministic test for one documented protocol-frame edge case,
then implement the smallest source correction it proves necessary. Own only
tests/test_protocol.py and src/hccast_wired/protocol.py. Do not use network or remote
services. Do not access hardware. Do not install or synchronize packages. Do not
initialize Git, create a repository, push, or publish. Do not change unrelated files.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_protocol.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the final UNIT-TESTED claim.
```

### Task 2: Live configuration validation test

#### Scope

Add one serialization or validation regression test for a documented local-only
live configuration constraint and make only the source correction proven necessary.

#### Owned files

`tests/live/test_model.py` and, only if the test proves it necessary,
`src/hccast_wired/live/model.py`.

#### Forbidden actions

Within this worker assignment, do not use network or remote services; access
hardware; install or synchronize packages; initialize Git or create repositories; push or publish; launch the
controller or subprocesses; or change unrelated files.

#### Prerequisites

Use the isolated development environment provisioned by the integrator with the
project's `dev` extra. If unavailable, report the missing dependency or environment
to the integrator; environment preparation is authorized within PB-1.
Read `AGENTS.md`, `src/hccast_wired/live/model.py`, and
`tests/live/test_model.py` before editing.

#### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/live/test_model.py`

#### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the final `UNIT-TESTED` claim.

#### Copy-ready brief

```text
Worker scope: this is an assigned software-only legacy task under AGENTS.md.
Use the integrator-provided isolated environment with the project dev extra. Report
missing dependencies to the integrator for resolution within PB-1. Do not use SSH,
network discovery or physical streams, change Pi configuration, or delegate work.

Write one failing LiveConfig serialization or validation regression test, then
implement the smallest source correction it proves necessary. Own only
tests/live/test_model.py and src/hccast_wired/live/model.py. Do not use network or
remote services. Do not access hardware or launch the controller or subprocesses.
Do not install or synchronize packages. Do not initialize Git, create a repository,
push, or publish. Do not change unrelated files.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/live/test_model.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the final UNIT-TESTED claim.
```

### Task 3: Public-control contract tightening

#### Scope

Add one deterministic repository test for an already approved public control; do
not change the control's policy or production behavior.

#### Owned files

`tests/test_public_repository.py` only.

#### Forbidden actions

Within this worker assignment, do not use network or remote services; access
hardware; install or synchronize packages; initialize Git or create repositories; push or publish; edit production
source or public policy; or change unrelated files.

#### Prerequisites

Use the isolated development environment provisioned by the integrator with the
project's `dev` extra. If unavailable, report the missing dependency or environment
to the integrator; environment preparation is authorized within PB-1.
Read `AGENTS.md`, `MODEL_CONTEXT.md`, and
`tests/test_public_repository.py` before editing.

#### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_public_repository.py`

#### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the exact public control protected under the `UNIT-TESTED` claim.

#### Copy-ready brief

```text
Worker scope: this is an assigned software-only legacy task under AGENTS.md.
Use the integrator-provided isolated environment with the project dev extra. Report
missing dependencies to the integrator for resolution within PB-1. Do not use SSH,
network discovery or physical streams, change Pi configuration, or delegate work.

Add one deterministic test that protects an existing approved public control. Own
only tests/test_public_repository.py. Do not use network or remote services. Do not
access hardware. Do not install or synchronize packages. Do not initialize Git,
create a repository, push, or publish. Do not edit production source or public policy.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_public_repository.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the exact public control protected under the UNIT-TESTED claim.
```

### Task 4: Direct-AOA FunctionFS descriptor regression

#### Scope

Add one deterministic regression test for a documented direct-AOA FunctionFS
descriptor or endpoint invariant relevant to Raspberry Pi portability, and make
only the smallest source correction proven necessary by that test.

#### Owned files

`tests/test_functionfs.py` and, only if the test proves it necessary,
`src/hccast_wired/functionfs.py`.

#### Forbidden actions

Within this worker assignment, do not use network or remote services; access
hardware; install or synchronize packages; initialize Git or create repositories; push or publish; run ConfigFS or
FunctionFS against a real UDC; edit platform-specific command builders; or change
unrelated files.

#### Prerequisites

Use the isolated development environment provisioned by the integrator with the
project's `dev` extra. If unavailable, report the missing dependency or environment
to the integrator; environment preparation is authorized within PB-1.
Read `AGENTS.md`, `docs/ARCHITECTURE.md`,
`src/hccast_wired/functionfs.py`, and `tests/test_functionfs.py` before editing.

#### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_functionfs.py`

#### Required evidence

Report the RED command and output, the GREEN command and output, files changed,
and the exact direct-AOA descriptor or endpoint invariant protected under the
`UNIT-TESTED` claim.

#### Copy-ready brief

```text
Worker scope: this is an assigned software-only legacy task under AGENTS.md.
Use the integrator-provided isolated environment with the project dev extra. Report
missing dependencies to the integrator for resolution within PB-1. Do not use SSH,
network discovery or physical streams, change Pi configuration, or delegate work.

Write one failing deterministic regression test for a documented direct-AOA
FunctionFS descriptor or endpoint invariant relevant to Raspberry Pi portability,
then implement the smallest source correction it proves necessary. Own only
tests/test_functionfs.py and src/hccast_wired/functionfs.py. Do not use network or
remote services. Do not access hardware or a real UDC. Do not install or synchronize
packages. Do not initialize Git, create a repository, push, or publish. Do not edit
platform-specific command builders or change unrelated files.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_functionfs.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the exact direct-AOA descriptor or endpoint invariant protected under the UNIT-TESTED claim.
```
