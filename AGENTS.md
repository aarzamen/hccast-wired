# HCCAST Wired agent contract

This repository is an experimental technical alpha for owned-hardware
interoperability. It is not clinical software and does not grant authority to
operate connected hardware.

## Authority and workflow

Read `README.md`, `docs/ARCHITECTURE.md`, `docs/VALIDATION.md`, the relevant
tests, and the assigned task before editing. `AGENTS.md` is the binding policy;
`MODEL_CONTEXT.md` is orientation only. Change only files explicitly assigned to
the task, work test-first, and report the exact verification commands and output.

Use isolated `uv` environments. Preserve Python 3.10 for the known-good Jetson
runtime; prefer Python 3.12 or 3.13 for development. Never install into system
Python or provide bare package-installer guidance.

The reviewed explicit allowlist is the publication boundary. `.gitignore` is
convenience and defense in depth, not the publication boundary. Keep raw logs,
media, private evidence, credentials, local paths, and internal planning private.

## Claim labels

Use one of these labels for technical claims:

- `OBSERVED` — directly recorded from a device, descriptor, APK, packaging, or run.
- `INFERRED` — a plausible explanation that is not directly proved.
- `IMPLEMENTED` — present in source.
- `UNIT-TESTED` — exercised without physical hardware.
- `HARDWARE-VERIFIED` — exercised on the named physical unit/platform with evidence.
- `REPRODUCED` — independently repeated on another physical unit or platform.

No current claim is `REPRODUCED`. A test-pattern preview is media evidence, not
protocol proof. Unsupported-platform work stays explicitly experimental.

## Default-deny actions

Agents have no authority to perform the following actions by default:

- Use any network or remote service.
- Initialize Git or create a GitHub or other remote repository.
- Push commits, create releases, or publish any source or artifact.
- Contact vendor services, operate a vendor cloud, or download opaque binaries.
- Update firmware or perform firmware operations.
- Run SSH, USB, ConfigFS, FunctionFS, systemd, `sudo`, privileged hardware actions, or destructive cleanup.
- Install packages or synchronize an environment.

A physically present human must explicitly authorize the exact action for the current
task before any item above is allowed. That authorization is narrow, temporary, and
never standing authority.

## Authorized hardware checkpoints

For any later authorized gadget experiment, perform fresh UDC discovery rather than
reusing a value from a log, record the isolation state, and preserve/verify
restoration of NVIDIA's stock `l4t` gadget after custom gadget work. Do not infer
protocol success from a rendered test pattern. Completing one authorized checkpoint
does not authorize later hardware work.

## Review discipline

Do not strengthen a claim from source, a unit test, or a historical run. Keep
protocol assumptions reviewable, test a change before implementation, and run the
full relevant software suite after it. Hardware-dependent work remains blocked
until a human supplies a specific, physical checkpoint authorization.
