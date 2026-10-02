# Contributing

Read [AGENTS.md](AGENTS.md) and the assigned task before editing. The approved
PB-1 manifest governs the current wireless-first PanelBridge build under
`apps/panelbridge/`; it preserves the existing HCCAST driver and evidence.
Preparation, isolated dependency setup, tests, fixes and restoration within that
manifest are already authorized. Firmware, EEPROM, publication, credential
changes and work outside the manifest require fresh approval.

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
At most two workers may run concurrently, with no recursive delegation. Active
USB protocol experiments and USB-video implementation are deferred under PB-1.

Keep raw evidence, media, vendor material, personal identifiers, absolute personal
paths and credentials out of public exports. Preserve existing MIT notices and
audit dependency licenses before selecting the new app's license. Use an explicit
publication allowlist and review every selected file for secrets and identifiers;
`.gitignore` alone is not a release boundary. Build approval does not authorize
publishing source, artifacts or releases.
