from __future__ import annotations

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]

PUBLIC_CANDIDATE_TEXT_FILES = (
    ".github/workflows/ci.yml",
    ".gitignore",
    ".gitattributes",
    "AGENTS.md",
    "CLAUDE.md",
    "MODEL_CONTEXT.md",
    "CONTRIBUTING.md",
    "ROADMAP.md",
    "THIRD_PARTY_NOTICES.md",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "pyproject.toml",
    "uv.lock",
    "docs/AGENT_TASKS.md",
    "docs/ARCHITECTURE.md",
    "docs/FIRST_RUN.md",
    "docs/REPRODUCTION.md",
    "docs/REVERSE_ENGINEERING.md",
    "docs/RK-X40F_MANUAL_FINDINGS.md",
    "docs/TESTED_HARDWARE.md",
    "docs/TEST_PLAN.md",
    "docs/VALIDATION.md",
    "docs/WHATCABLE.md",
    "docs/lab/2026-07-first-pixels.md",
    "scripts/capture-macos-host-claim.sh",
    "scripts/capture-macos-passive-attach.sh",
    "scripts/capture-macos-setr-once.sh",
    "scripts/capture-whatcable-macos.sh",
    "scripts/generate-test-pattern.sh",
    "scripts/probe-platform.sh",
)

REQUIRED_TASK_SECTIONS = (
    "Scope",
    "Owned files",
    "Forbidden actions",
    "Prerequisites",
    "Acceptance tests",
    "Required evidence",
    "Copy-ready brief",
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _normalized_prose(value: str) -> str:
    return " ".join(value.split())


def _markdown_section(document: str, heading: str, *, level: int) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{marker} {re.escape(heading)}\n\n(?P<body>.*?)(?=^#{{1,{level}}} |\Z)",
        document,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing {marker} {heading} section"
    return match.group("body").strip()


def _agent_tasks(document: str) -> dict[str, dict[str, str]]:
    task_matches = list(re.finditer(r"^## (Task \d+: [^\n]+)$", document, re.MULTILINE))
    parsed: dict[str, dict[str, str]] = {}
    for index, match in enumerate(task_matches):
        end = task_matches[index + 1].start() if index + 1 < len(task_matches) else len(document)
        task = document[match.end() : end]
        sections = re.findall(
            r"^### ([^\n]+)\n\n(.*?)(?=^### |\Z)",
            task,
            flags=re.MULTILINE | re.DOTALL,
        )
        parsed[match.group(1)] = {heading: body.strip() for heading, body in sections}
    return parsed


def _public_candidate_paths() -> tuple[str, ...]:
    python_paths = tuple(
        sorted(
            path.relative_to(ROOT).as_posix()
            for base in (ROOT / "src" / "hccast_wired", ROOT / "tests")
            for path in base.rglob("*.py")
        )
    )
    return PUBLIC_CANDIDATE_TEXT_FILES + python_paths


def test_public_control_files_exist() -> None:
    required = (
        ".github/workflows/ci.yml",
        ".gitignore",
        ".gitattributes",
        "AGENTS.md",
        "CLAUDE.md",
        "MODEL_CONTEXT.md",
        "CONTRIBUTING.md",
        "ROADMAP.md",
        "THIRD_PARTY_NOTICES.md",
        "docs/AGENT_TASKS.md",
    )

    assert [path for path in required if not (ROOT / path).is_file()] == []


def test_ci_is_read_only_pinned_and_covers_supported_python() -> None:
    workflow = _read(".github/workflows/ci.yml")

    assert "permissions:\n  contents: read" in workflow
    assert 'python-version: ["3.10", "3.12", "3.13"]' in workflow
    for required in (
        "uv sync --frozen --extra dev",
        "uv run --no-sync pytest -p no:cacheprovider -o addopts= -q",
        "uv run --no-sync ruff check src tests",
        "uv run --no-sync mypy src/hccast_wired/live",
        "uv build --out-dir dist",
        "hccast-wired --help",
    ):
        assert required in workflow

    action_uses = re.findall(r"^\s*uses:\s*([^@\s]+)@([^\s#]+)", workflow, re.MULTILINE)
    assert {name for name, _ in action_uses} == {
        "actions/checkout",
        "actions/setup-python",
        "astral-sh/setup-uv",
    }
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for _, revision in action_uses)
    assert "# v7.0.1" in workflow
    assert "# v7.0.0" in workflow
    assert "# v9.0.0" in workflow


def test_agent_contract_structurally_sets_claim_and_workflow_boundaries() -> None:
    agents = _read("AGENTS.md")

    authority = _markdown_section(agents, "Authority and workflow", level=2)
    normalized_authority = _normalized_prose(authority)
    assert "`AGENTS.md` is the binding policy" in normalized_authority
    assert "work test-first" in normalized_authority
    assert "Use isolated `uv` environments" in normalized_authority

    claims = _markdown_section(agents, "Claim labels", level=2)
    claim_labels = tuple(re.findall(r"^- `([A-Z-]+)` —", claims, re.MULTILINE))
    assert claim_labels == (
        "OBSERVED",
        "INFERRED",
        "IMPLEMENTED",
        "UNIT-TESTED",
        "HARDWARE-VERIFIED",
        "REPRODUCED",
    )
    normalized_claims = _normalized_prose(claims)
    assert "No current claim is `REPRODUCED`." in normalized_claims
    assert "test-pattern preview is media evidence, not protocol proof" in normalized_claims


def test_agent_contract_default_denies_external_and_publication_authority() -> None:
    agents = _read("AGENTS.md")
    default_deny = _markdown_section(agents, "Default-deny actions", level=2)
    normalized_default_deny = _normalized_prose(default_deny)

    assert (
        "Agents have no authority to perform the following actions by default:"
        in normalized_default_deny
    )
    assert tuple(re.findall(r"^- (.+)$", default_deny, re.MULTILINE)) == (
        "Use any network or remote service.",
        "Initialize Git or create a GitHub or other remote repository.",
        "Push commits, create releases, or publish any source or artifact.",
        "Contact vendor services, operate a vendor cloud, or download opaque binaries.",
        "Update firmware or perform firmware operations.",
        "Run SSH, USB, ConfigFS, FunctionFS, systemd, `sudo`, privileged hardware actions, or destructive cleanup.",
        "Install packages or synchronize an environment.",
    )
    assert (
        "A physically present human must explicitly authorize the exact action for the current "
        "task before any item above is allowed."
    ) in normalized_default_deny
    assert "never standing authority" in normalized_default_deny

    checkpoints = _markdown_section(agents, "Authorized hardware checkpoints", level=2)
    normalized_checkpoints = _normalized_prose(checkpoints)
    assert "fresh UDC discovery" in normalized_checkpoints
    assert "stock `l4t` gadget" in normalized_checkpoints
    assert "does not authorize later hardware work" in normalized_checkpoints


def test_model_context_orients_without_repeating_authoritative_policy() -> None:
    model_context = _read("MODEL_CONTEXT.md")

    assert model_context.startswith(
        "# Model context\n\nThis file provides status orientation only. "
        "[AGENTS.md](AGENTS.md) is authoritative."
    )
    assert "source_user" in model_context
    assert "`hccast`" in model_context
    assert not re.search(
        r"\b(?:agents? (?:must|may|shall)|do not|never|forbidden|required|authori[sz]e)\b",
        model_context,
        re.IGNORECASE,
    )
    assert "## " not in model_context


def test_claude_is_a_concise_fable_operating_map_without_standing_authority() -> None:
    claude = _read("CLAUDE.md")

    assert claude.startswith("# HCCAST Wired — Claude Code orientation\n")
    normalized = _normalized_prose(claude)
    for required in (
        "[AGENTS.md](AGENTS.md) is authoritative",
        "[README.md](README.md)",
        "[MODEL_CONTEXT.md](MODEL_CONTEXT.md)",
        "[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)",
        "[docs/VALIDATION.md](docs/VALIDATION.md)",
        "Raspberry Pi reproduction is the active target",
        "Jetson Orin Nano is the hardware-verified reference",
        "macOS is a development and diagnostic controller",
        "uv run --no-sync pytest -p no:cacheprovider -o addopts= -q",
        "uv run --no-sync ruff check src tests",
        "uv run --no-sync mypy src/hccast_wired/live",
        "python3 -m compileall -q src tests",
        "Preserve the user-selected model",
        "report the change before substantive work resumes",
    ):
        assert required in normalized

    assert not re.search(
        r"\b(?:standing authority|always authorized|no approval needed|silently delegate)\b",
        claude,
        re.IGNORECASE,
    )


def test_current_public_status_points_to_raspberry_pi_reproduction() -> None:
    readme = _read("README.md")
    model_context = _normalized_prose(_read("MODEL_CONTEXT.md"))
    roadmap = _normalized_prose(_read("ROADMAP.md"))
    validation = _read("docs/VALIDATION.md")
    first_run = _normalized_prose(_read("docs/FIRST_RUN.md"))

    for path in (
        "CLAUDE.md",
        "MODEL_CONTEXT.md",
        "ROADMAP.md",
        "docs/VALIDATION.md",
        "docs/TESTED_HARDWARE.md",
        "docs/REPRODUCTION.md",
        "docs/AGENT_TASKS.md",
    ):
        assert f"]({path})" in readme

    assert "Raspberry Pi reproduction" in model_context
    assert "macOS is diagnostic-only" in model_context
    assert "Raspberry Pi reproduction" in roadmap
    assert "FunctionFS against a real UDC" not in validation
    assert "Software-only verification" in first_run
    assert "Raspberry Pi target" in first_run
    assert "requires a separate, explicit bounded authorization" in first_run


def test_agent_task_queue_is_copyable_and_fully_bounded() -> None:
    document = _read("docs/AGENT_TASKS.md")
    tasks = _agent_tasks(document)

    assert len(tasks) == 4
    assert "Hardware work is blocked" in _normalized_prose(document)
    for title, sections in tasks.items():
        assert tuple(sections) == REQUIRED_TASK_SECTIONS, title
        assert all(sections.values()), title

        forbidden = sections["Forbidden actions"].lower()
        for boundary in ("network", "remote", "hardware", "package", "git", "publish"):
            assert boundary in forbidden, f"{title} missing {boundary} prohibition"

        prerequisites = sections["Prerequisites"]
        normalized_prerequisites = _normalized_prose(prerequisites)
        assert (
            "A human has already provisioned the isolated development environment"
            in normalized_prerequisites
        )
        assert "stop and request separate setup authorization" in normalized_prerequisites

        acceptance = sections["Acceptance tests"]
        assert re.fullmatch(
            r"`uv run --no-sync pytest -p no:cacheprovider -o addopts= -q [A-Za-z0-9_./-]+`",
            acceptance,
        ), f"{title} has a non-concrete or non-software acceptance gate"
        assert "UV_CACHE_DIR" not in acceptance
        assert "/private/" not in acceptance

        evidence = sections["Required evidence"]
        assert "RED" in evidence and "GREEN" in evidence
        assert "command" in evidence.lower() and "output" in evidence.lower()

        brief = sections["Copy-ready brief"]
        assert brief.startswith("```text\nPrerequisite: a human has already provisioned")
        normalized_brief = _normalized_prose(brief)
        assert "stop and request separate setup authorization" in normalized_brief
        assert "Do not install or synchronize packages" in normalized_brief
        assert "uv sync" not in brief
        assert "UV_CACHE_DIR" not in brief
        assert "/private/" not in brief


def test_third_party_notice_limits_mit_to_original_work() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md").lower()

    assert "mit" in notices
    assert "original" in notices
    assert "not affiliated" in notices


def test_public_metadata_credits_the_author_and_preserves_supported_versions() -> None:
    pyproject = _read("pyproject.toml")
    license_text = _read("LICENSE")

    assert 'version = "0.2.0"' in pyproject
    assert 'requires-python = ">=3.10"' in pyproject
    assert 'python_version = "3.10"' in pyproject
    assert "Aaron Arzamendi" in pyproject
    assert "Copyright (c) 2026 Aaron Arzamendi" in license_text


def test_ignore_rules_exclude_private_inventory_and_legacy_runners() -> None:
    ignored = _read(".gitignore")

    for required in (
        ".venv/",
        ".pytest_cache/",
        "__pycache__/",
        "build/",
        "dist/",
        "*.egg-info/",
        ".superpowers/",
        "docs/superpowers/",
        "logs/",
        "evidence/",
        "output/",
        "tmp/",
        ".DS_Store",
        ".env",
        "MANIFEST.sha256",
        "scripts/install.sh",
        "scripts/pipe-jetson-gstreamer.sh",
        "scripts/pipe-x11-ffmpeg.sh",
        "scripts/run-direct-aoa-test.sh",
        "scripts/run-gadget-handshake.sh",
        "scripts/run-gadget-test.sh",
        "scripts/start-xvfb-ui.sh",
    ):
        assert required in ignored
    assert "not the publication boundary" in _read("AGENTS.md")


def test_gitattributes_normalizes_text_and_keeps_media_binary() -> None:
    attributes = _read(".gitattributes")

    assert "* text=auto eol=lf" in attributes
    assert "*.png binary" in attributes
    assert "*.mp4 binary" in attributes


def test_explicit_public_candidate_has_no_bare_pip_guidance() -> None:
    package_tool = "p" + "ip"
    prohibited = re.compile(
        rf"(?<!uv\s)(?:python\s+-m\s+)?{package_tool}\s+install",
        re.IGNORECASE,
    )
    paths = _public_candidate_paths()

    assert "README.md" in paths
    assert "src/hccast_wired/host_usb.py" in paths
    assert "tests/test_public_repository.py" in paths
    assert all((ROOT / path).is_file() for path in paths)
    offenders = [path for path in paths if prohibited.search(_read(path))]
    assert offenders == []
