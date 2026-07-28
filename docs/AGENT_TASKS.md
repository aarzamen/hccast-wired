# Bounded software tasks

These tasks are standalone software-only briefs. Read `AGENTS.md` first. Hardware
work is blocked: none authorizes external I/O or changes to hardware state.

## Task 1: Protocol-frame regression coverage

### Scope

Add deterministic unit coverage for one documented protocol-frame edge case and
make only the smallest source correction proven necessary by that test.

### Owned files

`tests/test_protocol.py` and, only if the test proves it necessary,
`src/hccast_wired/protocol.py`.

### Forbidden actions

Do not use network or remote services; access hardware; install or synchronize
packages; initialize Git or create repositories; push or publish; or change
unrelated files.

### Prerequisites

A human has already provisioned the isolated development environment with the
project's `dev` extra. If it is unavailable, stop and request separate setup
authorization. Read `AGENTS.md`, `src/hccast_wired/protocol.py`, and
`tests/test_protocol.py` before editing.

### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_protocol.py`

### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the final `UNIT-TESTED` claim.

### Copy-ready brief

```text
Prerequisite: a human has already provisioned the isolated development environment
with the project dev extra. If it is unavailable, stop and request separate setup
authorization.

Write a failing deterministic test for one documented protocol-frame edge case,
then implement the smallest source correction it proves necessary. Own only
tests/test_protocol.py and src/hccast_wired/protocol.py. Do not use network or remote
services. Do not access hardware. Do not install or synchronize packages. Do not
initialize Git, create a repository, push, or publish. Do not change unrelated files.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_protocol.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the final UNIT-TESTED claim.
```

## Task 2: Live configuration validation test

### Scope

Add one serialization or validation regression test for a documented local-only
live configuration constraint and make only the source correction proven necessary.

### Owned files

`tests/live/test_model.py` and, only if the test proves it necessary,
`src/hccast_wired/live/model.py`.

### Forbidden actions

Do not use network or remote services; access hardware; install or synchronize
packages; initialize Git or create repositories; push or publish; launch the
controller or subprocesses; or change unrelated files.

### Prerequisites

A human has already provisioned the isolated development environment with the
project's `dev` extra. If it is unavailable, stop and request separate setup
authorization. Read `AGENTS.md`, `src/hccast_wired/live/model.py`, and
`tests/live/test_model.py` before editing.

### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/live/test_model.py`

### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the final `UNIT-TESTED` claim.

### Copy-ready brief

```text
Prerequisite: a human has already provisioned the isolated development environment
with the project dev extra. If it is unavailable, stop and request separate setup
authorization.

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

## Task 3: Public-control contract tightening

### Scope

Add one deterministic repository test for an already approved public control; do
not change the control's policy or production behavior.

### Owned files

`tests/test_public_repository.py` only.

### Forbidden actions

Do not use network or remote services; access hardware; install or synchronize
packages; initialize Git or create repositories; push or publish; edit production
source or public policy; or change unrelated files.

### Prerequisites

A human has already provisioned the isolated development environment with the
project's `dev` extra. If it is unavailable, stop and request separate setup
authorization. Read `AGENTS.md`, `MODEL_CONTEXT.md`, and
`tests/test_public_repository.py` before editing.

### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_public_repository.py`

### Required evidence

Report the RED command and output, the GREEN command and output, files changed, and
the exact public control protected under the `UNIT-TESTED` claim.

### Copy-ready brief

```text
Prerequisite: a human has already provisioned the isolated development environment
with the project dev extra. If it is unavailable, stop and request separate setup
authorization.

Add one deterministic test that protects an existing approved public control. Own
only tests/test_public_repository.py. Do not use network or remote services. Do not
access hardware. Do not install or synchronize packages. Do not initialize Git,
create a repository, push, or publish. Do not edit production source or public policy.

Acceptance: uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_public_repository.py
Evidence: report the RED command/output, GREEN command/output, files changed, and
the exact public control protected under the UNIT-TESTED claim.
```

## Task 4: Direct-AOA FunctionFS descriptor regression

### Scope

Add one deterministic regression test for a documented direct-AOA FunctionFS
descriptor or endpoint invariant relevant to Raspberry Pi portability, and make
only the smallest source correction proven necessary by that test.

### Owned files

`tests/test_functionfs.py` and, only if the test proves it necessary,
`src/hccast_wired/functionfs.py`.

### Forbidden actions

Do not use network or remote services; access hardware; install or synchronize
packages; initialize Git or create repositories; push or publish; run ConfigFS or
FunctionFS against a real UDC; edit platform-specific command builders; or change
unrelated files.

### Prerequisites

A human has already provisioned the isolated development environment with the
project's `dev` extra. If it is unavailable, stop and request separate setup
authorization. Read `AGENTS.md`, `docs/ARCHITECTURE.md`,
`src/hccast_wired/functionfs.py`, and `tests/test_functionfs.py` before editing.

### Acceptance tests

`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests/test_functionfs.py`

### Required evidence

Report the RED command and output, the GREEN command and output, files changed,
and the exact direct-AOA descriptor or endpoint invariant protected under the
`UNIT-TESTED` claim.

### Copy-ready brief

```text
Prerequisite: a human has already provisioned the isolated development environment
with the project dev extra. If it is unavailable, stop and request separate setup
authorization.

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
