from __future__ import annotations

from pathlib import Path
import re
import shlex


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

REQUIRED_LEGACY_TASK_SECTIONS = (
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


def _markdown_section(document: str, heading: str, *, level: int) -> str:
    marker = "#" * level
    match = re.search(
        rf"^{marker} (?:\d+\. )?{re.escape(heading)}\n\n(?P<body>.*?)(?=^#{{1,{level}}} |\Z)",
        document,
        flags=re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"missing {marker} {heading} section"
    return match.group("body").strip()


def _legacy_agent_tasks(document: str) -> dict[str, dict[str, str]]:
    document = _markdown_section(document, "Optional legacy briefs", level=2)
    task_matches = list(re.finditer(r"^### (Task \d+: [^\n]+)$", document, re.MULTILINE))
    parsed: dict[str, dict[str, str]] = {}
    for index, match in enumerate(task_matches):
        end = task_matches[index + 1].start() if index + 1 < len(task_matches) else len(document)
        task = document[match.end() : end]
        sections = re.findall(
            r"^#### ([^\n]+)\n\n(.*?)(?=^#### |\Z)",
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


def test_agent_contract_defines_the_evidence_claim_vocabulary() -> None:
    claims = _markdown_section(_read("AGENTS.md"), "Claim labels", level=3)
    # Labels are the shared evidence vocabulary; wording and current results may evolve.
    claim_labels = re.findall(r"^- (?:`|\*\*)([A-Z-]+)(?:`|\*\*) —", claims, re.MULTILINE)

    assert len(claim_labels) == len(set(claim_labels))
    assert set(claim_labels) == {
        "OBSERVED",
        "INFERRED",
        "IMPLEMENTED",
        "UNIT-TESTED",
        "HARDWARE-VERIFIED",
        "REPRODUCED",
    }


def test_agent_contract_keeps_high_risk_actions_outside_build_approval() -> None:
    boundaries = _markdown_section(_read("AGENTS.md"), "Always request fresh approval", level=2)
    # Keep these categories in the fresh-approval boundary without freezing sentences.
    for category in ("firmware", "EEPROM", "publishing", "credentials", "reimaging"):
        assert re.search(rf"\b{category}\b", boundaries, re.IGNORECASE), category


def test_orientation_documents_link_to_the_authoritative_contract() -> None:
    for relative_path in ("CLAUDE.md", "MODEL_CONTEXT.md", "CONTRIBUTING.md", "docs/AGENT_TASKS.md"):
        document = _read(relative_path)
        links = re.findall(r"\[AGENTS\.md\]\(([^)]+)\)", document)
        assert links, f"{relative_path} must point readers to the binding contract"
        for target in links:
            assert (ROOT / relative_path).parent.joinpath(target).resolve() == ROOT / "AGENTS.md"


def test_claude_operating_map_links_resolve() -> None:
    claude = _read("CLAUDE.md")
    links = re.findall(r"\[[^]\n]+\]\(([^)]+)\)", claude)

    assert {"AGENTS.md", "MODEL_CONTEXT.md", "README.md"} <= set(links)
    for target in links:
        assert (ROOT / target).is_file(), f"broken CLAUDE.md link: {target}"


def test_legacy_readme_evidence_links_remain_available() -> None:
    readme = _read("README.md")

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
        assert (ROOT / path).is_file()


def test_panelbridge_lane_table_covers_stages_and_planned_component_ownership() -> None:
    active = _markdown_section(_read("docs/AGENT_TASKS.md"), "Active PB-1 lanes", level=2)
    table = [line for line in active.splitlines() if line.startswith("| ")]
    header = [column.strip() for column in table[0].strip("|").split("|")]
    assert header == ["Stage", "Integrator", "Worker A ownership", "Worker B ownership", "Gate"]
    rows = [[column.strip() for column in line.strip("|").split("|")] for line in table[1:]]
    assert len(rows) == 5
    assert {row[0].split(".", 1)[0] for row in rows} == {"0", "1", "2", "3", "4"}
    assert all(len(row) == 5 and all(row) for row in rows)

    # These are planned ownership boundaries, not assertions that code already exists.
    worker_paths = set(re.findall(r"`(apps/panelbridge/[^`]+)`", " ".join(
        cell for row in rows for cell in row[2:4]
    )))
    assert {
        "apps/panelbridge/native/capture/",
        "apps/panelbridge/native/wfd/",
        "apps/panelbridge/panelbridge/controller/",
        "apps/panelbridge/helper/",
        "apps/panelbridge/panelbridge/ui/",
        "apps/panelbridge/packaging/",
        "apps/panelbridge/tests/",
        "apps/panelbridge/docs/",
    } <= worker_paths
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in worker_paths)


def test_documented_existing_python_checks_name_real_test_targets() -> None:
    active = _markdown_section(_read("docs/AGENT_TASKS.md"), "Active PB-1 lanes", level=2)
    checks = _markdown_section(active, "Existing Python checks", level=3)
    blocks = re.findall(r"```bash\n(.*?)\n```", checks, re.DOTALL)
    assert blocks
    for block in blocks:
        for command in block.splitlines():
            arguments = shlex.split(command)
            assert arguments[:4] == [".venv/bin/python", "-B", "-m", "pytest"]
            assert "-q" in arguments
            targets = arguments[arguments.index("-q") + 1:]
            assert targets
            assert all((ROOT / target).is_file() for target in targets)
            assert all(target.startswith(("tests/", "apps/panelbridge/tests/")) for target in targets)


def test_legacy_worker_briefs_have_bounded_files_and_runnable_acceptance() -> None:
    tasks = _legacy_agent_tasks(_read("docs/AGENT_TASKS.md"))

    assert tasks
    for title, sections in tasks.items():
        assert set(REQUIRED_LEGACY_TASK_SECTIONS) <= set(sections), title
        assert all(sections[heading] for heading in REQUIRED_LEGACY_TASK_SECTIONS), title

        owned = re.findall(r"`([^`]+\.py)`", sections["Owned files"])
        assert owned, f"{title} must assign concrete files"
        assert all((ROOT / path).is_file() for path in owned), title

        forbidden = sections["Forbidden actions"].lower()
        for boundary in ("worker", "network", "remote", "hardware", "package", "git", "publish"):
            assert boundary in forbidden, f"{title} missing {boundary} boundary"

        command = sections["Acceptance tests"].strip("`")
        arguments = shlex.split(command)
        assert arguments[:8] == [
            "uv", "run", "--no-sync", "pytest", "-p", "no:cacheprovider", "-o", "addopts=",
        ], title
        assert arguments[8] == "-q", title
        test_paths = arguments[9:]
        assert test_paths and set(test_paths) <= set(owned), title
        assert all(path.startswith("tests/") and (ROOT / path).is_file() for path in test_paths)

        brief = sections["Copy-ready brief"]
        assert brief.startswith("```text\n") and brief.endswith("```"), title
        assert f"Acceptance: {command}" in brief, title
        for path in owned:
            assert path in brief, f"{title} copy-ready brief omits owned file {path}"
        assert "AGENTS.md" in brief and "integrator" in brief, title


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
