from __future__ import annotations

from pathlib import Path
import re
import shlex
import subprocess
import tarfile


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

    # One version source: pyproject reads hccast_wired.__version__ at build time.
    assert 'dynamic = ["version"]' in pyproject
    assert 'version = {attr = "hccast_wired.__version__"}' in pyproject
    assert re.search(r'^version = "', pyproject, re.MULTILINE) is None
    package_init = _read("src/hccast_wired/__init__.py")
    assert re.search(r'^__version__ = "[0-9]+\.[0-9]+\.[0-9]+"$', package_init, re.MULTILINE)
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


# --- Publication manifest scan -------------------------------------------------
#
# The named-file policy tests above protect specific controls. The checks below
# inspect the actual publication inventory (every Git-tracked file, including
# the PanelBridge subtree and vendored archive member names) for personal
# identifiers, credential signatures and credential- or patient-looking files.
# Sensitive literals are assembled from fragments so this file never contains a
# string that its own scan would flag. Offenders are reported by path, label and
# line only; matched text is never echoed.

VENDORED_ARCHIVE = "apps/panelbridge/vendor/gnome-network-displays-0.99.0.tar.gz"
WALK_EXCLUDED_PARTS = frozenset({
    ".git", ".venv", ".venv-smoke", "__pycache__", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "dist", "build", "output", "evidence", "logs", "tmp",
    ".superpowers", ".release", "node_modules",
})
BINARY_SUFFIXES = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".mp4", ".mov", ".pdf",
    ".zip", ".gz", ".bin", ".der", ".apk",
)


def _joined(*fragments: str) -> str:
    return "".join(fragments)


CREDENTIAL_FILENAME = re.compile(
    r"(?i)(?:"
    r"\.(?:pem|key|p12|pfx|jks|keystore|ppk)$"
    r"|(?:^|/)id_(?:rsa|dsa|ecdsa|ed25519)"
    r"|(?:^|/)\.(?:env|netrc|npmrc|pypirc|htpasswd)(?:\.|$)"
    r"|credential|secret|passw(?:or)?d|kubeconfig"
    r"|(?:^|[^a-z])token(?:[^a-z]|$)"
    r")"
)
PATIENT_FILENAME = re.compile(
    r"(?i)(?:"
    + _joined("pat", "ient")
    + "|(?:^|[^a-z])" + _joined("p", "hi") + "(?:[^a-z]|$)"
    + "|(?:^|[^a-z])" + _joined("m", "rn") + "(?:[^a-z]|$)"
    + r"|\.(?:" + _joined("h", "l7") + "|" + _joined("d", "cm") + ")$"
    + "|" + _joined("dic", "om")
    + "|" + _joined("sick", "[-_ ]?call")
    + "|(?:^|[^a-z])" + _joined("so", "ap") + "(?:[^a-z]|$)"
    + "|" + _joined("ros", "ter")
    + "|(?:^|[^a-z])" + _joined("d", "ob") + "(?:[^a-z]|$)"
    + "|(?:^|[^a-z])" + _joined("s", "sn") + "(?:[^a-z]|$)"
    + "|" + _joined("medi", "cal") + "|" + _joined("clin", "ic")
    + r")"
)
TEXT_SIGNATURES = {
    # Unix or macOS home directories name a person; template placeholders such
    # as "/home/{user}" are not identifiers.
    "personal-home-path": re.compile(r"(?<![\w/])/(?:Users|home)/(?![{$<])[A-Za-z0-9._-]+"),
    "windows-home-path": re.compile(r"(?i)[a-z]:[\\/]+Users[\\/]+[^\s\"'<>\\/]+"),
    "private-key-block": re.compile(
        _joined("-----BEGIN ", r"(?:[A-Z0-9]+ )?", "PRIVATE", " KEY-----")
    ),
    "aws-access-key": re.compile(_joined(r"\b", "AK", "IA", r"[0-9A-Z]{16}\b")),
    "github-token": re.compile(_joined(r"\bgh", "[pousr]", r"_[A-Za-z0-9]{30,}\b")),
    "slack-token": re.compile(_joined(r"\bxo", "x[baprs]-", r"[A-Za-z0-9-]{10,}")),
    "credential-assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|secret[_-]?key|passw(?:or)?d)\b\s*[:=]\s*[\"']?[^\s\"']{6,}"
    ),
    "mac-address": re.compile(
        r"(?i)(?<![0-9a-f])(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|(?:[0-9a-f]{4}\.){2}[0-9a-f]{4})"
        r"(?![0-9a-f])"
    ),
    "email-address": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "private-lan-address": re.compile(
        r"\b(?:192\.168|10|172\.(?:1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}\b"
    ),
    "patient-record-field": re.compile(
        r"(?i)\b(?:" + "|".join((
            _joined("m", "rn"), _joined("s", "sn"), _joined("d", "ob"),
            _joined("pat", "ient name"), _joined("date of ", "birth"), _joined("hip", "aa"),
        )) + r")\b"
    ),
}
# Exactly the reviewed synthetic namespaces, mirroring the PanelBridge source
# bundle's export gate: local-unicast fixture addresses in tests and one
# invalid-multicast rejection fixture. Nothing else is exempt.
SYNTHETIC_MAC_PREFIX = "0200000000"
MULTICAST_REJECTION_FIXTURE = ("apps/panelbridge/tests/test_rescue_service.py", "030000000001")
# Documentation-reserved domains only; nothing that could route mail to a person.
DOCUMENTATION_EMAIL_DOMAINS = re.compile(r"(?i)(?:^|\.)example\.(?:test|invalid|com|org|net)$|\.(?:test|invalid)$")
# A receiver's own factory metadata URL, used as a parser fixture; it is a
# device default, not a host binding.
DEVICE_DEFAULT_ADDRESS_FIXTURES = {
    "tests/test_protocol.py": {".".join(("192", "168", "203", "1"))},
}


def _publication_manifest() -> tuple[str, ...]:
    """Return every tracked file, or a filtered tree walk when Git is unavailable."""

    try:
        completed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files", "-z", "--cached"],
            capture_output=True,
            check=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return tuple(sorted(
            path.relative_to(ROOT).as_posix()
            for path in ROOT.rglob("*")
            if path.is_file() and not (set(path.relative_to(ROOT).parts) & WALK_EXCLUDED_PARTS)
        ))
    return tuple(sorted(name for name in completed.stdout.decode("utf-8").split("\0") if name))


def _publication_text(relative_path: str) -> str | None:
    if relative_path.lower().endswith(BINARY_SUFFIXES):
        return None
    return (ROOT / relative_path).read_bytes().decode("utf-8")


def _signature_allowed(label: str, relative_path: str, matched: str) -> bool:
    if label == "mac-address":
        compact = re.sub(r"[:.\-]", "", matched).lower()
        in_tests = relative_path.startswith(("tests/", "apps/panelbridge/tests/"))
        return (in_tests and compact.startswith(SYNTHETIC_MAC_PREFIX)) or (
            (relative_path, compact) == MULTICAST_REJECTION_FIXTURE
        )
    if label == "email-address":
        return DOCUMENTATION_EMAIL_DOMAINS.search(matched.rsplit("@", 1)[1]) is not None
    if label == "private-lan-address":
        return matched in DEVICE_DEFAULT_ADDRESS_FIXTURES.get(relative_path, set())
    return False


def test_publication_manifest_is_the_tracked_tree_and_includes_panelbridge() -> None:
    manifest = _publication_manifest()

    assert len(manifest) > 100
    assert set(PUBLIC_CANDIDATE_TEXT_FILES) <= set(manifest)
    assert {
        "apps/panelbridge/README.md",
        "apps/panelbridge/THIRD_PARTY_NOTICES.md",
        "apps/panelbridge/packaging_tools/source_bundle.py",
        "apps/panelbridge/packaging/installer-bootstrap.py",
        "src/hccast_wired/live/checkpoint.py",
        VENDORED_ARCHIVE,
    } <= set(manifest)
    assert all((ROOT / path).is_file() for path in manifest)
    assert all(not Path(path).is_absolute() and ".." not in Path(path).parts for path in manifest)


def test_publication_manifest_names_no_credential_or_patient_looking_files() -> None:
    manifest = _publication_manifest()

    offenders = [
        path for path in manifest
        if CREDENTIAL_FILENAME.search(path) or PATIENT_FILENAME.search(path)
    ]
    assert offenders == []

    with tarfile.open(ROOT / VENDORED_ARCHIVE, "r:gz") as archive:
        member_names = [member.name for member in archive.getmembers()]
    assert member_names
    assert [
        name for name in member_names
        if CREDENTIAL_FILENAME.search(name) or PATIENT_FILENAME.search(name)
    ] == []


def test_publication_manifest_text_is_utf8_without_hidden_binary() -> None:
    offenders = []
    for path in _publication_manifest():
        if path.lower().endswith(BINARY_SUFFIXES):
            continue
        data = (ROOT / path).read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            offenders.append((path, "not-utf8"))
        if b"\0" in data:
            offenders.append((path, "nul-byte"))
    assert offenders == []


def test_publication_manifest_text_carries_no_identifiers_or_credential_signatures() -> None:
    offenders = []
    scanned = 0
    for path in _publication_manifest():
        text = _publication_text(path)
        if text is None:
            continue
        scanned += 1
        for label, pattern in TEXT_SIGNATURES.items():
            for match in pattern.finditer(text):
                if _signature_allowed(label, path, match.group()):
                    continue
                offenders.append((path, label, text.count("\n", 0, match.start()) + 1))
    assert scanned > 100
    assert offenders == []


def test_publication_scan_signatures_detect_their_targets() -> None:
    # The scan is only as good as its patterns; prove each one fires on a
    # synthetic sample assembled here, never on a stored secret.
    samples = {
        "personal-home-path": "/" + "Users" + "/" + "someone" + "/Projects",
        "windows-home-path": "C:" + "\\Users\\" + "someone",
        "private-key-block": _joined("-----BEGIN ", "RSA ", "PRIVATE", " KEY-----"),
        "aws-access-key": _joined("AK", "IA", "ABCDEFGHIJKLMNOP"),
        "github-token": _joined("gh", "p", "_", "a" * 36),
        "slack-token": _joined("xo", "x", "b", "-", "1234567890-abc"),
        "credential-assignment": _joined("pass", "word", " = ", "hunter22"),
        "mac-address": ":".join(("a4", "b1", "c2", "d3", "e4", "f5")),
        "email-address": "someone" + "@" + "corp" + "-mail" + ".net",
        "private-lan-address": ".".join(("192", "168", "1", "20")),
        "patient-record-field": _joined("Pat", "ient Name: "),
    }
    for label, sample in samples.items():
        assert TEXT_SIGNATURES[label].search(sample), label
        assert not _signature_allowed(label, "docs/README.md", sample), label
    assert _signature_allowed(
        "email-address", "docs/README.md", "noreply" + "@" + "example" + ".invalid"
    )
    assert _signature_allowed("mac-address", "tests/test_x.py", ":".join(("02", "00", "00", "00", "00", "01")))
    assert not _signature_allowed("mac-address", "src/x.py", ":".join(("02", "00", "00", "00", "00", "01")))
    assert CREDENTIAL_FILENAME.search("deploy/id_" + "rsa")
    assert CREDENTIAL_FILENAME.search("config/" + "secrets" + ".json")
    assert PATIENT_FILENAME.search("notes/" + "pat" + "ient-list.csv")
    assert PATIENT_FILENAME.search("records/scan." + "d" + "cm")
    assert not PATIENT_FILENAME.search("apps/panelbridge/panelbridge/credits.py")
    assert not CREDENTIAL_FILENAME.search("apps/panelbridge/packaging_tools/runtime_access.py")
