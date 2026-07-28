# Contributing

This repository accepts small, reviewable software changes. Read `AGENTS.md` and
the task-specific documentation before editing; its authority and hardware gate
apply to every contribution.

Set up an isolated development environment from a source checkout:

```bash
uv sync --extra dev
uv run pytest
uv run ruff check src tests
uv run mypy src/hccast_wired/live
```

Use test-first changes and run the narrowest relevant test before the broader
software checks. Include changed files, commands, outputs, and claim labels in the
handoff. Do not add raw evidence, media, vendor material, machine-specific paths,
or credentials. Hardware work needs a separate, explicitly authorized physical
checkpoint and cannot be inferred from a software contribution.
