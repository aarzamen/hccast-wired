"""Sanitized recorded-style apt-get simulation fixtures, not raw Pi captures."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools.apt_plan import AptPlanError, inspect_install_plan


VERSION = "0.1.0~dev1"
APP_INST = "Inst panelbridge (0.1.0~dev1 local-deb [arm64])"
APP_CONF = "Conf panelbridge (0.1.0~dev1 local-deb [arm64])"
MINIMAL = f"0 upgraded, 1 newly installed, 0 to remove and 17 not upgraded.\n{APP_INST}\n{APP_CONF}\n"
WITH_DEPENDENCY = """Reading package lists...
Building dependency tree...
Reading state information...
Note, selecting 'panelbridge' instead of './panelbridge_0.1.0~dev1_arm64.deb'
The following additional packages will be installed:
  python3-cairo
Suggested packages:
  python3-cairo-dev
The following NEW packages will be installed:
  panelbridge python3-cairo
0 upgraded, 2 newly installed, 0 to remove and 17 not upgraded.
Inst python3-cairo (1.27.0-2 Debian:13.1/stable [arm64])
Inst panelbridge (0.1.0~dev1 local-deb [arm64])
Conf python3-cairo (1.27.0-2 Debian:13.1/stable [arm64])
Conf panelbridge (0.1.0~dev1 local-deb [arm64])
"""


def inspect(text=MINIMAL, installed=None, version=VERSION):
    return inspect_install_plan(text, expected_version=version,
                                installed_versions={} if installed is None else installed)


def test_first_install_on_existing_runtime_returns_only_app_addition():
    installed = {"python3": "3.13.5-1", "python3-cairo": "1.27.0-2",
                 "network-manager": "1.52.1-1+rpt4", "libc6:arm64": "2.41-12+rpt1"}
    plan = inspect(installed=installed)
    assert [(p.package, p.version, p.architecture) for p in plan.additions] == [
        ("panelbridge", "0.1.0~dev1", "arm64")]
    assert plan.not_upgraded == 17
    installed["panelbridge"] = VERSION
    assert len(plan.additions) == 1
    with pytest.raises(FrozenInstanceError):
        plan.not_upgraded = 0
    with pytest.raises(FrozenInstanceError):
        plan.additions[0].version = "2"


def test_dependency_additions_preserve_exact_versions_and_install_order():
    plan = inspect(WITH_DEPENDENCY, {"python3": "3.13.5-1", "dbus": "1.16.2-2"})
    assert [(p.package, p.version, p.architecture) for p in plan.additions] == [
        ("python3-cairo", "1.27.0-2", "arm64"), ("panelbridge", VERSION, "arm64")]


def test_arm64_qualified_names_match_unqualified_configuration_rows():
    plan = inspect(WITH_DEPENDENCY.replace("Inst python3-cairo (", "Inst python3-cairo:arm64 ("))
    assert plan.additions[0].package == "python3-cairo"


def test_architecture_independent_dependency_and_epoch_version():
    text = WITH_DEPENDENCY.replace("1.27.0-2", "2:1.27.0~rc1+deb13u1-2")
    text = text.replace("Debian:13.1/stable [arm64]", "Debian:13.1/stable [all]")
    plan = inspect(text)
    assert (plan.additions[0].version, plan.additions[0].architecture) == (
        "2:1.27.0~rc1+deb13u1-2", "all")


def test_interleaved_install_and_configure_rows_are_complete():
    text = WITH_DEPENDENCY.replace(f"{APP_INST}\nConf python3-cairo (1.27.0-2 Debian:13.1/stable [arm64])",
                                   f"Conf python3-cairo (1.27.0-2 Debian:13.1/stable [arm64])\n{APP_INST}")
    assert len(inspect(text).additions) == 2


def test_kept_back_and_autoremove_suggestions_do_not_count_as_changes():
    preamble = """The following packages were automatically installed and are no longer required:
  old-library
Use 'sudo apt autoremove' to remove them.
The following packages have been kept back:
  network-manager
"""
    plan = inspect(preamble + MINIMAL, {"old-library": "1.0-1", "network-manager": "1.52.1-1+rpt4"})
    assert [p.package for p in plan.additions] == ["panelbridge"]


@pytest.mark.parametrize("version", ["1.27.0-1", "1.27.0-2", "1.27.0-3"])
@pytest.mark.parametrize("name", ["python3-cairo", "python3-cairo:arm64", "python3-cairo:armhf"])
def test_any_existing_dependency_is_rejected_even_without_old_version_row(name, version):
    with pytest.raises(AptPlanError):
        inspect(WITH_DEPENDENCY, {name: version})


@pytest.mark.parametrize("name", ["panelbridge", "panelbridge:arm64", "panelbridge:armhf"])
def test_existing_app_cannot_be_reinstalled_or_upgraded(name):
    with pytest.raises(AptPlanError):
        inspect(installed={name: "0.0.1"})


@pytest.mark.parametrize("row", [
    "Remv network-manager [1.52.1-1+rpt4]",
    "Purg network-manager [1.52.1-1+rpt4]",
    "Inst python3-cairo [1.26.0-1] (1.27.0-2 Debian:13.1/stable [arm64])",
    "Conf network-manager (1.52.1-1+rpt4 Debian:13.1/stable [arm64])",
])
def test_undeclared_removal_upgrade_or_existing_configuration_row_is_rejected(row):
    with pytest.raises(AptPlanError):
        inspect(MINIMAL + row + "\n")


@pytest.mark.parametrize("summary", [
    "1 upgraded, 1 newly installed, 0 to remove and 17 not upgraded.",
    "0 upgraded, 1 newly installed, 1 to remove and 17 not upgraded.",
    "0 upgraded, 1 newly installed, 1 reinstalled, 0 to remove and 17 not upgraded.",
    "0 upgraded, 1 newly installed, 1 downgraded, 0 to remove and 17 not upgraded.",
    "0 upgraded, 0 newly installed, 0 to remove and 17 not upgraded.",
    "0 upgraded, 2 newly installed, 0 to remove and 17 not upgraded.",
    "0 upgraded, -1 newly installed, 0 to remove and 17 not upgraded.",
])
def test_summary_must_describe_exact_addition_rows(summary):
    with pytest.raises(AptPlanError):
        inspect(summary + f"\n{APP_INST}\n{APP_CONF}\n")


@pytest.mark.parametrize("text", [
    "", APP_INST + "\n" + APP_CONF,
    MINIMAL.splitlines()[0] + "\n",
    MINIMAL.replace(APP_CONF + "\n", ""),
    MINIMAL.replace(APP_INST + "\n", ""),
    MINIMAL.replace(APP_INST, APP_INST + "\n" + APP_INST),
    MINIMAL + APP_CONF + "\n",
    MINIMAL + MINIMAL,
    MINIMAL.replace(APP_INST, APP_CONF).replace(APP_CONF + "\n" + APP_CONF, APP_CONF + "\n" + APP_INST),
    MINIMAL.replace("Conf panelbridge (0.1.0~dev1", "Conf panelbridge (0.1.0~dev2"),
    MINIMAL.replace("Conf panelbridge (0.1.0~dev1 local-deb", "Conf panelbridge (0.1.0~dev1 Other:13/stable"),
    MINIMAL.replace("Inst panelbridge (", "Inst panelbridge:arm64 (") + APP_INST + "\n",
    MINIMAL.replace("panelbridge", "different-package"),
])
def test_incomplete_duplicate_conflicting_or_missing_app_actions_are_rejected(text):
    with pytest.raises(AptPlanError):
        inspect(text)


@pytest.mark.parametrize("failure", [
    "E: Unmet dependencies. Try 'apt --fix-broken install' with no packages.",
    "E: Unable to correct problems, you have held broken packages.",
    "W: The following packages cannot be authenticated!",
    "1 not fully installed or removed.",
    "Broken panelbridge:arm64 Depends on python3-cairo:arm64 < none >",
    "Conf panelbridge (0.1.0~dev1 local-deb [arm64]) [broken-package:arm64 ]",
    "Fetched 20 kB in 1s (20 kB/s)",
    "Something from an unknown APT version",
])
def test_failure_or_unknown_output_is_not_ignored_even_after_complete_rows(failure):
    with pytest.raises(AptPlanError):
        inspect(MINIMAL + failure + "\n")


@pytest.mark.parametrize("before,after", [
    ("Inst panelbridge (", "Inst  panelbridge ("),
    ("Inst panelbridge (", " Inst panelbridge ("),
    ("Inst panelbridge (", "INST panelbridge ("),
    ("Inst panelbridge (", "Install panelbridge ("),
    ("[arm64]", "[amd64]"),
    ("[arm64]", "[all]"),
    ("panelbridge (", "panelbridge:all ("),
    ("[arm64]", "arm64"),
    ("local-deb [", "["),
    ("local-deb", "local-deb (unexpected)"),
    ("local-deb", "local-deb "),
    ("0.1.0~dev1", "bad-version"),
    ("Inst panelbridge", "Inst ../panelbridge"),
    ("Inst panelbridge", "Inst PanelBridge"),
    ("Inst panelbridge", "Inst panelbridge:any"),
    ("\nConf", "\r\nConf"),
    ("\nConf", "\x1b[0m\nConf"),
    ("\nConf", "\x00\nConf"),
    ("local-deb", "lócál-deb"),
])
def test_unknown_action_syntax_architectures_or_control_bytes_fail_closed(before, after):
    with pytest.raises(AptPlanError):
        inspect(MINIMAL.replace(before, after))


def test_wrong_expected_app_version_is_rejected():
    with pytest.raises(AptPlanError):
        inspect(version="0.1.0~dev2")


@pytest.mark.parametrize("text", [
    WITH_DEPENDENCY.replace("  panelbridge python3-cairo", "  panelbridge"),
    WITH_DEPENDENCY.replace("  panelbridge python3-cairo", "  panelbridge python3-cairo dbus"),
    WITH_DEPENDENCY.replace("  panelbridge python3-cairo", "  panelbridge panelbridge python3-cairo"),
    WITH_DEPENDENCY.replace("  panelbridge python3-cairo", ""),
    WITH_DEPENDENCY.replace("  python3-cairo\nSuggested", "  dbus\nSuggested"),
    WITH_DEPENDENCY.replace("The following NEW packages", "The following upgraded packages"),
    "  panelbridge python3-cairo\n" + MINIMAL,
    WITH_DEPENDENCY + "The following NEW packages will be installed:\n  panelbridge\n",
    "The following packages have been kept back:\n  panelbridge\n" + MINIMAL,
])
def test_package_lists_must_agree_with_action_rows(text):
    with pytest.raises(AptPlanError):
        inspect(text)


def test_additional_list_cannot_omit_one_of_the_added_dependencies():
    text = WITH_DEPENDENCY.replace("  panelbridge python3-cairo", "  panelbridge python3-cairo dbus")
    text = text.replace("2 newly installed", "3 newly installed")
    text += "Inst dbus (1.16.2-2 Debian:13.1/stable [arm64])\n"
    text += "Conf dbus (1.16.2-2 Debian:13.1/stable [arm64])\n"
    with pytest.raises(AptPlanError):
        inspect(text)


def test_simulation_privilege_notice_is_informational():
    notice = """NOTE: This is only a simulation!
      apt-get needs root privileges for real execution.
      Keep also in mind that locking is deactivated,
      so don't depend on the relevance to the real current situation!
"""
    assert inspect(notice + MINIMAL).additions[0].package == "panelbridge"


def test_summary_rejects_an_excessive_dependency_transaction():
    with pytest.raises(AptPlanError):
        inspect(MINIMAL.replace("1 newly installed", "513 newly installed"))


@pytest.mark.parametrize("text", [MINIMAL + " " * (256 * 1024), MINIMAL + "\n" * 8193,
                                 " " * 4097 + "\n" + MINIMAL], ids=["bytes", "lines", "line-bytes"])
def test_oversized_transcript_or_line_count_or_line_fails(text):
    with pytest.raises(AptPlanError):
        inspect(text)


@pytest.mark.parametrize("installed", [[], {"bad name": "1"}, {"python3": None},
                                       {"python3": "3\n"}, {"python3": "x"}])
def test_malformed_installed_inventory_is_rejected(installed):
    with pytest.raises(AptPlanError):
        inspect(installed=installed)


@pytest.mark.parametrize("version", [None, "", "1\n", "x", "1 2", "1" * 257,
                                     "1-", "1:2-", "1_1", "1:"])
def test_invalid_expected_version_cannot_become_a_success(version):
    with pytest.raises(AptPlanError):
        inspect(version=version)


@pytest.mark.parametrize("version", ["1-", "1:2-", "1_1", "1:"])
def test_malformed_debian_dependency_versions_are_rejected(version):
    with pytest.raises(AptPlanError):
        inspect(WITH_DEPENDENCY.replace("1.27.0-2", version))


def test_errors_do_not_echo_private_origin_or_input_paths():
    text = MINIMAL + "E: Could not open file /private/example/no-export\n"
    with pytest.raises(AptPlanError) as failure:
        inspect(text)
    assert "no-export" not in str(failure.value)
    assert "/private/" not in str(failure.value)
