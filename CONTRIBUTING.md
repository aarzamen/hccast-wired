# Contributing

Read [AGENTS.md](AGENTS.md), the current [scope](docs/SCOPE.md) and the assigned
task before editing. PanelBridge lives under `apps/panelbridge/`; preserve the
legacy HCCAST driver and device-specific evidence. Private hardware approvals
are not standing authorization for contributors. Firmware, EEPROM, credential
changes and work outside the current assignment need separate authorization.

Use the existing isolated development environment. If a new environment is
needed, use `uv` with Python 3.12 or 3.13 in a task-specific location and preserve
the working environment. From an already provisioned checkout root:

```bash
.venv/bin/python -B -m pytest -p no:cacheprovider -o addopts= -q
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
```

Run focused tests for the behavior changed, then relevant broader checks. Include
changed files, exact commands, outputs, exit results and claim labels in the
handoff. Passing software tests does not establish physical playback or product
readiness.

The integrator holds the sole hardware lease and owns shared interfaces. Workers
stay within their assigned files and software-only scope; they send environment
blockers to the integrator without creating a new user approval requirement.
At most two workers may run concurrently, with no recursive delegation. USB
protocol experiments require a separate host/device assignment; the current
PanelBridge use case is wireless.

Keep raw evidence, private media metadata, vendor firmware/APKs, personal
identifiers, absolute personal paths and credentials out of public exports.
Owner-approved demonstration media needs metadata/visual review and accurate
captions. Preserve the legacy MIT and PanelBridge GPL-3.0-or-later licenses and
upstream notices. Use an explicit publication allowlist and review every selected file for secrets and identifiers;
`.gitignore` alone is not a release boundary. Build approval does not authorize
publishing source, artifacts or releases.
