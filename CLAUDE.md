# PanelBridge and HCCAST — agent orientation

Read [AGENTS.md](AGENTS.md) first, then [MODEL_CONTEXT.md](MODEL_CONTEXT.md),
[README.md](README.md), the assigned source and tests. The public source snapshot
contains experimental PanelBridge work alongside the legacy HCCAST driver.
Private host bindings, approvals, raw evidence and development plans are excluded.
Obtain the current human assignment before any physical or system operation.

PanelBridge lives under `apps/panelbridge/`. It uses GTK4, the normal labwc desktop
and Wi-Fi Direct/Miracast. EBPSI USB video is unavailable. Preserve the modular
transport boundary, normal desktop startup and recovery behavior; improve
performance with measured comparisons. Optional OLED installation is deferred.

[docs/AGENT_TASKS.md](docs/AGENT_TASKS.md) describes component ownership and
software checks. One integrator owns hardware, SSH, system configuration and
shared interfaces. At most two workers run concurrently with distinct files.
Workers do not access physical hardware or recursively delegate.

Use an isolated uv environment for Mac development. Product packaging uses distro
Python/GI and compiled helpers. Legacy checks run from the checkout root:

```bash
uv run --no-sync pytest -p no:cacheprovider -o addopts= -q tests
uv run --no-sync ruff check src tests
uv run --no-sync mypy src/hccast_wired/live
```

Focused PanelBridge checks run against `apps/panelbridge/tests/`. Native sender
and Linux package checks require their documented dependencies and platform.
Software checks do not prove visible playback, headless boot or recovery.

Preserve existing Git history, MIT/GPL notices, failed evidence and known-good
runtimes. Export only reviewed source and sanitized summaries. A current explicit
publication assignment authorizes its push; it does not authorize firmware,
credentials, hardware experiments or changes to repository visibility.
