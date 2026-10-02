"""Pure first-install validation of bounded LC_ALL=C apt-get simulation output.

Supported action grammar is ``Inst|Conf name[:arch] (version origin [arch])``;
only arm64/all additions are accepted, and PanelBridge itself must be arm64.
Every Inst needs a later matching Conf. There must be one traditional apt-get
summary, and any NEW/additional/kept-back package lists must agree with actions.
Known informational lines may precede the summary; unknown formats fail closed.
APT's newer ``apt`` display format, progress/control bytes, warnings, removals,
old-version annotations, broken-state annotations and reconfiguration of an
already installed package are deliberately unsupported.

The caller must obtain a complete successful simulation (including checking its
exit status and stderr), verify the local package separately, and supply a fresh
complete installed package/version inventory. The returned plan does not prove
origin/authenticity, repository configuration or package-manager health. Origin
labels are compared for consistency only, then discarded. This module does no
I/O and accepts no command/path arguments.

APT resolution and installed state can change after inspection. The transaction
still needs fresh revalidation, package-manager locking and --no-remove plus
no-upgrade policy; this result is not a race-free installation authorization.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import re


MAX_TEXT_BYTES = 256 * 1024
MAX_LINES = 8192
MAX_LINE_BYTES = 4096
MAX_ADDITIONS = 512
MAX_INSTALLED_PACKAGES = 100_000

_NAME = r"[a-z0-9][a-z0-9+.-]+"
_ARCH = r"[a-z0-9][a-z0-9-]*"
_PACKAGE = re.compile(rf"(?P<name>{_NAME})(?::(?P<arch>{_ARCH}))?")
_VERSION = re.compile(r"(?:[0-9]+:)?[0-9][A-Za-z0-9.+~\-]*")
_ACTION = re.compile(
    rf"(?P<action>Inst|Conf) (?P<package>{_NAME}(?::{_ARCH})?) "
    r"\((?P<version>[^ ()]+) (?P<origin>[A-Za-z0-9][A-Za-z0-9 .,:/+_~=%@!\-]*) "
    rf"\[(?P<arch>{_ARCH})\]\)"
)
_SUMMARY = re.compile(
    r"(?P<upgraded>[0-9]{1,6}) upgraded, (?P<new>[0-9]{1,6}) newly installed, "
    r"(?P<removed>[0-9]{1,6}) to remove and (?P<held>[0-9]{1,6}) not upgraded\."
)
_SELECTION = re.compile(r"Note, selecting 'panelbridge' instead of '[^']+'")
_LIST_HEADERS = {
    "The following additional packages will be installed:": "additional",
    "The following NEW packages will be installed:": "new",
    "The following packages have been kept back:": "kept",
    "The following packages were automatically installed and are no longer required:": "unused",
    "Suggested packages:": "suggested",
    "Recommended packages:": "recommended",
}
_INFORMATION = {
    "Reading package lists...", "Reading package lists... Done",
    "Building dependency tree...", "Building dependency tree... Done",
    "Reading state information...", "Reading state information... Done",
    "NOTE: This is only a simulation!",
    "      apt-get needs root privileges for real execution.",
    "      Keep also in mind that locking is deactivated,",
    "      so don't depend on the relevance to the real current situation!",
}
_AUTOREMOVE_NOTICES = {
    f"Use '{command} autoremove' to remove them."
    for command in ("apt", "apt-get", "sudo apt", "sudo apt-get")
}


class AptPlanError(ValueError):
    """The simulation is unsupported or outside first-install policy."""


@dataclass(frozen=True)
class PackageAddition:
    package: str
    version: str
    architecture: str


@dataclass(frozen=True)
class InstallPlan:
    additions: tuple[PackageAddition, ...]
    not_upgraded: int


def _valid_version(value):
    return (isinstance(value, str) and 1 <= len(value) <= 256
            and _VERSION.fullmatch(value) is not None and not value.endswith("-"))


def _package(value):
    if not isinstance(value, str) or not 2 <= len(value) <= 256:
        raise AptPlanError("Invalid package name")
    match = _PACKAGE.fullmatch(value)
    if match is None:
        raise AptPlanError("Invalid package name")
    return match["name"], match["arch"]


def _installed_names(installed_versions):
    if (not isinstance(installed_versions, Mapping)
            or len(installed_versions) > MAX_INSTALLED_PACKAGES):
        raise AptPlanError("Installed inventory must be a bounded package/version mapping")
    names = set()
    for package, version in installed_versions.items():
        name, _ = _package(package)
        if not _valid_version(version):
            raise AptPlanError("Invalid installed package version")
        # Conservatively reject a base-name collision across any architectures.
        names.add(name)
    return names


def inspect_install_plan(
    text: str, *, expected_version: str, installed_versions: Mapping[str, str]
) -> InstallPlan:
    """Return only validated additions, or raise AptPlanError without input echoes.

    ``installed_versions`` must contain every currently installed package, with
    plain or dpkg-style architecture-qualified names. Existing versions need not
    equal candidates: any attempted change to an existing base name is refused.
    ``not_upgraded`` is informational; it is not a set of scheduled mutations.
    """
    if (not isinstance(text, str) or len(text) > MAX_TEXT_BYTES
            or not text.isascii()
            or any(ord(char) < 32 and char != "\n" or ord(char) == 127 for char in text)):
        raise AptPlanError("APT output must be bounded ASCII text without control bytes")
    lines = text.split("\n")
    if len(lines) > MAX_LINES or any(len(line) > MAX_LINE_BYTES for line in lines):
        raise AptPlanError("APT output exceeds line bounds")
    if not _valid_version(expected_version):
        raise AptPlanError("Invalid expected PanelBridge version")
    installed = _installed_names(installed_versions)
    if "panelbridge" in installed:
        raise AptPlanError("PanelBridge is already installed; first-install plans only")

    summary = None
    lists = {}
    current_list = None
    installs = {}
    origins = {}
    configured = set()
    seen_information = set()
    for number, line in enumerate(lines, 1):
        if not line:
            continue
        if summary is None:
            if line in _INFORMATION:
                if line in seen_information:
                    raise AptPlanError(f"Duplicate informational line at line {number}")
                seen_information.add(line)
                continue
            if line.startswith("  ") and current_list is not None:
                for item in line.split():
                    name, arch = _package(item)
                    if arch not in (None, "arm64", "all") or name in lists[current_list]:
                        raise AptPlanError(f"Invalid or duplicate list entry at line {number}")
                    lists[current_list].add(name)
                continue
            if current_list is not None and not lists[current_list]:
                raise AptPlanError(f"Empty package list before line {number}")
            current_list = None
            if line in _LIST_HEADERS:
                current_list = _LIST_HEADERS[line]
                if current_list in lists:
                    raise AptPlanError(f"Duplicate package list at line {number}")
                lists[current_list] = set()
                continue
            if line in _AUTOREMOVE_NOTICES and "unused" in lists:
                continue
            if _SELECTION.fullmatch(line):
                if "selection" in seen_information:
                    raise AptPlanError(f"Duplicate package selection at line {number}")
                seen_information.add("selection")
                continue
            match = _SUMMARY.fullmatch(line)
            if match is not None:
                summary = {name: int(value) for name, value in match.groupdict().items()}
                if summary["upgraded"] or summary["removed"]:
                    raise AptPlanError("APT plans upgrades or removals")
                if not 1 <= summary["new"] <= MAX_ADDITIONS:
                    raise AptPlanError("APT addition count is outside first-install bounds")
                continue
            raise AptPlanError(f"Unsupported APT output before summary at line {number}")

        match = _ACTION.fullmatch(line)
        if match is None:
            raise AptPlanError(f"Unsupported APT action or diagnostic at line {number}")
        if match["origin"].endswith(" "):
            raise AptPlanError(f"Malformed origin field at line {number}")
        name, suffix = _package(match["package"])
        version, arch = match["version"], match["arch"]
        if not _valid_version(version) or arch not in ("arm64", "all"):
            raise AptPlanError(f"Invalid version or architecture at line {number}")
        if suffix is not None and suffix != arch:
            raise AptPlanError(f"Conflicting package architecture at line {number}")
        if name in installed:
            raise AptPlanError(f"APT would change an installed package at line {number}")
        if name == "panelbridge" and (version != expected_version or arch != "arm64"):
            raise AptPlanError("PanelBridge version or architecture does not match")
        addition = PackageAddition(name, version, arch)
        if match["action"] == "Inst":
            if name in installs:
                raise AptPlanError(f"Duplicate or conflicting install action at line {number}")
            if len(installs) >= MAX_ADDITIONS:
                raise AptPlanError("APT plan exceeds addition bounds")
            installs[name] = addition
            origins[name] = match["origin"]
        else:
            if name in configured or installs.get(name) != addition or origins.get(name) != match["origin"]:
                raise AptPlanError(f"Missing, duplicate or conflicting install/configure pair at line {number}")
            configured.add(name)

    if summary is None or "panelbridge" not in installs:
        raise AptPlanError("APT plan has no complete first-install summary for PanelBridge")
    names = set(installs)
    if len(installs) != summary["new"] or configured != names:
        raise AptPlanError("APT summary and complete install/configure actions do not match")
    if "new" in lists and lists["new"] != names:
        raise AptPlanError("APT NEW package list does not match actions")
    if "additional" in lists and lists["additional"] != names - {"panelbridge"}:
        raise AptPlanError("APT additional package list does not match actions")
    if lists.get("kept", set()) & names or len(lists.get("kept", set())) > summary["held"]:
        raise AptPlanError("APT kept-back package list conflicts with the plan")
    if lists.get("unused", set()) & names:
        raise AptPlanError("APT unused package list conflicts with new additions")
    return InstallPlan(tuple(installs.values()), summary["held"])
