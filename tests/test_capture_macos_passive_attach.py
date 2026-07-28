from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture-macos-passive-attach.sh"
WHATCABLE_DOC = PROJECT_ROOT / "docs" / "WHATCABLE.md"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip())
    path.chmod(0o755)


def test_passive_attach_script_exists_is_executable_and_has_valid_syntax() -> None:
    assert CAPTURE_SCRIPT.is_file()
    assert os.access(CAPTURE_SCRIPT, os.X_OK)
    completed = subprocess.run(
        ["bash", "-n", str(CAPTURE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_passive_attach_script_has_a_strict_observation_only_boundary() -> None:
    script = CAPTURE_SCRIPT.read_text()

    for required in (
        'default_observe_seconds="30"',
        'default_observe_seconds="45"',
        'observe_seconds="${OBSERVE_SECONDS:-$default_observe_seconds}"',
        'ioreg_poll_interval="${IOREG_POLL_INTERVAL:-0.05}"',
        "whatcable --watch --json",
        "ioreg -r -c IOUSBHostDevice -l -w0",
        "system_profiler",
        "log show",
        "externally powered through its POWER port",
        "visibly stable on the QR/setup UI",
        "Connect DATA exactly once",
        "Do not power-cycle",
        "no PyUSB/libusb",
        "no interface claim",
        "no endpoint read/write",
        "no SETR",
        "no application-requested USB reset",
    ):
        assert required in script

    for forbidden in (
        ".venv/bin/hccast-wired",
        "LIBUSB_DEBUG",
        "host-claim",
        "host-setr-once",
        "host-stream",
        "gadget-stream",
        "--detach-kernel",
        "--set-configuration",
        "usb.core",
        "sudo ",
        "curl ",
        "wget ",
        'observe_seconds="${OBSERVE_SECONDS:-30}"',
    ):
        assert forbidden not in script

    precondition_read = script.index("PRECONDITION\nread -r")
    log_window_assignment = script.rindex(
        'log_query_start_local="$(date \'+%Y-%m-%d %H:%M:%S\')"'
    )
    assert precondition_read < log_window_assignment
    observation_sleep = script.rindex('sleep "$observe_seconds"')
    observer_readiness = script.rindex("wait_for_observers")
    observation_start = script.rindex(
        'observation_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"'
    )
    log_end_assignment = script.rindex(
        'log_query_end_local="$(date \'+%Y-%m-%d %H:%M:%S\')"'
    )
    finalization = script.rindex("finalize 0")
    assert observer_readiness < observation_start < observation_sleep
    assert observation_sleep < log_end_assignment < finalization


def test_whatcable_doc_exposes_the_passive_control_and_preconditions() -> None:
    doc = WHATCABLE_DOC.read_text()
    assert "scripts/capture-macos-passive-attach.sh" in doc
    assert "stable on the QR/setup UI" in doc
    assert "DATA disconnected" in doc
    assert "observation-only" in doc
    assert "30-second" in doc
    assert "BOOT_ACTION=short-press" in doc
    assert "BOOT_ACTION=long-press-5s" in doc
    assert "off-boot" in doc
    assert "deliberately powered off" in doc
    assert "45-second" in doc
    assert "does not authorize SETR" in doc


def _stage_fake_project(
    tmp_path: Path,
    *,
    mode: str,
    stubborn_watcher: bool = False,
    watch_early_exit: bool = False,
    log_fail: bool = False,
    main_sleep_fail: bool = False,
    use_default_observe: bool = False,
) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "fake-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    shutil.copy2(CAPTURE_SCRIPT, scripts / CAPTURE_SCRIPT.name)

    command_log = tmp_path / "commands.tsv"
    phase_file = tmp_path / "phase.txt"
    watch_stop_file = tmp_path / "whatcable-watch-stopped.txt"

    _write_executable(
        fake_bin / "sleep",
        r"""
        #!/usr/bin/env bash
        if [[ "${MAIN_SLEEP_FAIL:-0}" == "1" && "${1:-}" == "0.12" ]]; then
          exit 143
        fi
        if [[ "${1:-}" == "30" || "${1:-}" == "45" ]]; then
          exit 0
        fi
        exec /bin/sleep "$@"
        """,
    )
    _write_executable(
        fake_bin / "uname",
        r"""
        #!/usr/bin/env bash
        printf 'uname\t%s\n' "$*" >> "$COMMAND_LOG"
        if [[ "${1:-}" == "-s" ]]; then
          printf 'Darwin\n'
        else
          printf 'Darwin fake-mac 26.5.1 arm64\n'
        fi
        """,
    )
    _write_executable(
        fake_bin / "sw_vers",
        r"""
        #!/usr/bin/env bash
        printf 'sw_vers\t%s\n' "$*" >> "$COMMAND_LOG"
        printf 'ProductVersion:\t26.5.1\n'
        """,
    )
    _write_executable(
        fake_bin / "whatcable",
        r"""
        #!/usr/bin/env bash
        printf 'whatcable\t%s\n' "$*" >> "$COMMAND_LOG"
        case "${1:-}" in
          --json) printf '{"fake":"snapshot"}\n' ;;
          --raw)
            phase="${HCCAST_CAPTURE_PHASE:-$(cat "$PHASE_FILE" 2>/dev/null || true)}"
            if [[ "$phase" == "after" && "$MODE" == "usb2-fail" ]]; then
              printf 'Transport: USB 2.0 (480 Mbps), connected\n'
            elif [[ "$phase" == "before" && "$MODE" == "removal-only" ]]; then
              printf 'Transport: USB 2.0 (480 Mbps), connected\n'
            else
              printf 'No downstream USB transport\n'
            fi
            ;;
          --version) printf 'WhatCable fake 1.0\n' ;;
          --watch)
            printf '{"headline":"Connected","transportType":"USB2","scope":"baseline-hub"}\n'
            if [[ "${WATCH_EARLY_EXIT:-0}" == "1" ]]; then
              exit 9
            fi
            if [[ "${STUBBORN_WATCHER:-0}" == "1" ]]; then
              trap '' TERM
            else
              trap 'printf stopped > "$WATCH_STOP_FILE"; exit 0' TERM INT
            fi
            if [[ "$MODE" == "usb2-fail" ]]; then
              printf '{"transport":"USB 2.0","state":"attached"}\n'
            elif [[ "$MODE" == "attach-no-id" ]]; then
              printf '{"state":"attached"}\n'
            elif [[ "$MODE" == "negative-attach-words" ]]; then
              printf '{"connected":false,"state":"disconnected"}\n'
            fi
            while :; do sleep 0.02; done
            ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "system_profiler",
        r"""
        #!/usr/bin/env bash
        printf 'system_profiler\t%s\n' "$*" >> "$COMMAND_LOG"
        case "${1:-}" in
          -listDataTypes) printf 'SPUSBHostDataType\n' ;;
          SPUSBHostDataType)
            phase="${HCCAST_CAPTURE_PHASE:-$(cat "$PHASE_FILE" 2>/dev/null || true)}"
            if [[ "$phase" == "after" && "$MODE" == "usb2-fail" ]]; then
              printf 'USB 2.0 Bus: Up to 480 Mb/s\n'
            elif [[ "$phase" == "before" && "$MODE" == "removal-only" ]]; then
              printf 'USB 2.0 Bus: Up to 480 Mb/s\n'
            else
              printf 'FAKE_HOST_USB_TREE\n'
            fi
            ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "ioreg",
        r"""
        #!/usr/bin/env bash
        printf 'ioreg\t%s\n' "$*" >> "$COMMAND_LOG"
        phase="${HCCAST_CAPTURE_PHASE:-$(cat "$PHASE_FILE" 2>/dev/null || true)}"

        base() {
          cat <<'EOF'
+-o BaselineHub  <class IOUSBHostDevice>
  | {"idVendor" = 1452, "idProduct" = 32768}
EOF
        }
        target() {
          cat <<'EOF'
+-o HCCASTCandidate  <class IOUSBHostDevice>
  | {"idVendor" = 7358, "idProduct" = 5}
EOF
        }
        other() {
          cat <<'EOF'
+-o AlternatePersonality  <class IOUSBHostDevice>
  | {"idVendor" = 43981, "idProduct" = 2}
EOF
        }
        split_false_pair() {
          cat <<'EOF'
+-o VendorOnly  <class IOUSBHostDevice>
  | {"idVendor" = 43981, "idProduct" = 123}
+-o ProductOnly  <class IOUSBHostDevice>
  | {"idVendor" = 42, "idProduct" = 2}
EOF
        }

        base
        if [[ "$phase" == "watch" ]]; then
          case "$MODE" in
            persistent|transient) target ;;
            other) other ;;
            split-records) split_false_pair ;;
          esac
        elif [[ "$phase" == "after" ]]; then
          case "$MODE" in
            persistent) target ;;
            other) other ;;
            split-records) split_false_pair ;;
          esac
        fi
        """,
    )
    _write_executable(
        fake_bin / "log",
        r"""
        #!/usr/bin/env bash
        printf 'log\t%s\n' "$*" >> "$COMMAND_LOG"
        if [[ "${LOG_FAIL:-0}" == "1" ]]; then
          printf 'fake unified-log failure\n' >&2
          exit 9
        fi
        case "$MODE" in
          usb2-fail)
            printf '%s\n' \
              'kernel: USB2 device attached at high-speed' \
              'kernel: AppleUSBHostPort::setAddress: failed to set device address'
            ;;
          attach-no-id) printf 'kernel: USB device attached to port\n' ;;
          macos-host-connection-active)
            printf '%s\n' \
              'kernel: AppleHPMInterface - setting USB2 USB3 as DFP, as connected' \
              'kernel: IOPort::_updateConnectionActive_block_invoke(): m_connectionActive: YES'
            ;;
          negative-attach-words) printf 'kernel: USB device detached from port\n' ;;
          *) printf 'kernel: unrelated message\n' ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "rg",
        r"""
        #!/usr/bin/env bash
        printf 'rg\t%s\n' "$*" >> "$COMMAND_LOG"
        exec /usr/bin/grep -E "$@"
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "COMMAND_LOG": str(command_log),
            "PHASE_FILE": str(phase_file),
            "WATCH_STOP_FILE": str(watch_stop_file),
            "MODE": mode,
            "STUBBORN_WATCHER": "1" if stubborn_watcher else "0",
            "WATCH_EARLY_EXIT": "1" if watch_early_exit else "0",
            "LOG_FAIL": "1" if log_fail else "0",
            "MAIN_SLEEP_FAIL": "1" if main_sleep_fail else "0",
            "OBSERVE_SECONDS": "0.12",
            "IOREG_POLL_INTERVAL": "0.02",
            "WATCHER_STOP_ATTEMPTS": "2",
            "WATCHER_STOP_INTERVAL": "0.01",
        }
    )
    if use_default_observe:
        env.pop("OBSERVE_SECONDS", None)
    return project, env


def _run_staged(
    project: Path,
    env: dict[str, str],
    *,
    args: tuple[str, ...] = (),
    input_text: str = "\n",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "scripts" / CAPTURE_SCRIPT.name), *args],
        cwd=project,
        env=env,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _only_capture(project: Path) -> Path:
    capture_root = project / "logs" / "whatcable"
    captures = [
        *capture_root.glob("*-passive-attach"),
        *capture_root.glob("*-passive-off-boot"),
    ]
    assert len(captures) == 1
    return captures[0]


def _assert_passive_command_log(command_log: Path) -> None:
    commands = command_log.read_text()
    for expected in ("whatcable", "system_profiler", "ioreg", "log"):
        assert expected in commands
    for forbidden in (
        "hccast-wired",
        "libusb",
        "pyusb",
        "host-claim",
        "host-setr",
        "endpoint",
        "set-configuration",
    ):
        assert forbidden not in commands.lower()


def test_default_mode_remains_visible_ui(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    assert "visibly stable on the QR/setup UI" in completed.stdout
    capture = _only_capture(project)
    assert capture.name.endswith("-passive-attach")
    assert "start mode: visible-ui" in (capture / "environment.txt").read_text()
    assert "start mode: visible-ui" in (capture / "capture-result.txt").read_text()
    assert "- Start mode: **visible-ui**" in (capture / "SUMMARY.md").read_text()


@pytest.mark.parametrize("boot_action", ("short-press", "long-press-5s"))
def test_off_boot_mode_records_selected_boot_action(
    tmp_path: Path,
    boot_action: str,
) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none")
    env["BOOT_ACTION"] = boot_action

    completed = _run_staged(project, env, args=("off-boot",))

    assert completed.returncode == 0, completed.stderr
    assert "deliberately powered off" in completed.stdout
    assert "not an automatic timeout state" in completed.stdout
    assert completed.stdout.count("Connect DATA exactly once") == 1
    assert (
        completed.stdout.count(
            f"perform the recorded boot action exactly once: {boot_action}"
        )
        == 1
    )
    capture = _only_capture(project)
    assert capture.name.endswith("-passive-off-boot")
    for evidence_name in ("environment.txt", "capture-result.txt", "SUMMARY.md"):
        evidence = (capture / evidence_name).read_text()
        assert "off-boot" in evidence
        assert boot_action in evidence
    assert "observation seconds: 0.12" in (
        capture / "capture-result.txt"
    ).read_text()
    assert "- Observation window: **0.12 seconds**" in (
        capture / "SUMMARY.md"
    ).read_text()


@pytest.mark.parametrize(
    ("args", "boot_action", "stderr_fragment"),
    (
        (
            ("off-boot",),
            None,
            "BOOT_ACTION is required for off-boot mode.",
        ),
        (
            ("off-boot",),
            "double-click",
            "Invalid BOOT_ACTION: double-click",
        ),
        (
            ("unknown-mode",),
            None,
            "Invalid start mode: unknown-mode",
        ),
    ),
    ids=("missing-boot-action", "invalid-boot-action", "invalid-start-mode"),
)
def test_invalid_mode_contract_is_rejected_before_observation(
    tmp_path: Path,
    args: tuple[str, ...],
    boot_action: str | None,
    stderr_fragment: str,
) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none")
    env.pop("BOOT_ACTION", None)
    if boot_action is not None:
        env["BOOT_ACTION"] = boot_action

    completed = _run_staged(project, env, args=args, input_text="")

    assert completed.returncode == 2
    assert stderr_fragment in completed.stderr
    assert not (project / "logs" / "whatcable").exists()
    assert not Path(env["COMMAND_LOG"]).exists()


def test_off_boot_mode_defaults_to_45_seconds(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        mode="none",
        use_default_observe=True,
    )
    env["BOOT_ACTION"] = "short-press"

    completed = _run_staged(project, env, args=("off-boot",))

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert "observation seconds: 45" in (capture / "capture-result.txt").read_text()
    assert "- Observation window: **45 seconds**" in (
        capture / "SUMMARY.md"
    ).read_text()


def test_1cbe_present_after_window_is_classified_without_claiming_continuity(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(tmp_path, mode="persistent")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    assert "command not found" not in completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "TARGET_1CBE_0005_PRESENT_AFTER_WINDOW" in summary
    assert "`TARGET_1CBE_0005_PRESENT_AFTER_WINDOW` means" in summary
    assert "does not prove continuous presence" in summary
    assert "1cbe:0005" in (capture / "new-vidpid-unique.tsv").read_text()
    assert "1cbe:0005" in (capture / "ioreg-rapid-timeline.tsv").read_text()
    assert "HCCASTCandidate" in (capture / "ioreg-rapid-snapshots.txt").read_text()
    assert (capture / "whatcable-watch.ndjson").is_file()
    assert (capture / "system-profiler-before.txt").is_file()
    assert (capture / "system-profiler-after.txt").is_file()
    assert (capture / "system-profiler.diff").is_file()
    assert (capture / "ioreg.diff").is_file()
    assert (capture / "kernel-log.txt").is_file()
    assert "no interface claim" in summary
    assert "no endpoint read/write" in summary
    assert "no application-requested USB reset" in summary
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"
    _assert_passive_command_log(Path(env["COMMAND_LOG"]))


def test_transient_1cbe_is_distinct_from_persistent(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="transient")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "- Classification: **TARGET_1CBE_0005_TRANSIENT**" in summary
    assert (
        "- Classification: **TARGET_1CBE_0005_PRESENT_AFTER_WINDOW**"
        not in summary
    )
    assert "1cbe:0005" in (capture / "new-vidpid-unique.tsv").read_text()
    assert "1cbe:0005" not in (capture / "after-vidpid.tsv").read_text()


def test_other_new_vid_pid_is_reported_without_assuming_hccast(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="other")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "OTHER_VID_PID_OBSERVED" in summary
    assert "abcd:0002" in (capture / "new-vidpid-unique.tsv").read_text()
    assert "do not assume this is HCCAST" in summary


def test_usb2_attach_plus_address_failure_gets_specific_classification(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="usb2-fail")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "USB2_ATTACH_ADDRESS_STAGE_ENUMERATION_FAILURE" in summary
    result = (capture / "capture-result.txt").read_text()
    assert "usb2 attach evidence: yes" in result
    assert "address-stage failure evidence: yes" in result
    assert "new VID:PID count: 0" in result


def test_attach_without_id_is_not_misreported_as_no_attach(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="attach-no-id")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "USB_ATTACH_WITHOUT_DEVICE_ENUMERATION" in summary
    assert "NO_USB_ATTACH_OBSERVED" not in summary


def test_macos_host_connection_active_without_vid_pid_is_an_attach(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        mode="macos-host-connection-active",
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    result = (capture / "capture-result.txt").read_text()
    summary = (capture / "SUMMARY.md").read_text()
    assert "general attach evidence: yes" in result
    assert "usb2 attach evidence: no" in result
    assert "USB_ATTACH_WITHOUT_DEVICE_ENUMERATION" in summary
    assert "NO_USB_ATTACH_OBSERVED" not in summary


def test_no_attach_is_classified_without_inventing_a_device(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert "NO_USB_ATTACH_OBSERVED" in (capture / "SUMMARY.md").read_text()
    assert (capture / "new-vidpid-unique.tsv").read_text() == ""
    assert "usb2 attach evidence: no" in (capture / "capture-result.txt").read_text()


def test_negative_connection_words_do_not_count_as_attach(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="negative-attach-words")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "NO_USB_ATTACH_OBSERVED" in summary
    assert "USB_ATTACH_WITHOUT_DEVICE_ENUMERATION" not in summary


def test_removal_only_diff_does_not_count_as_new_usb2_attach(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="removal-only")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    result = (capture / "capture-result.txt").read_text()
    assert "NO_USB_ATTACH_OBSERVED" in (capture / "SUMMARY.md").read_text()
    assert "usb2 attach evidence: no" in result


def test_vendor_and_product_must_come_from_the_same_ioreg_service(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="split-records")

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    observed = (capture / "new-vidpid-unique.tsv").read_text()
    assert "abcd:0002" not in observed
    assert "abcd:007b" in observed
    assert "002a:0002" in observed


def test_stubborn_watchers_are_bounded_and_final_evidence_is_written(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none", stubborn_watcher=True)

    started = time.monotonic()
    completed = _run_staged(project, env)
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 5
    capture = _only_capture(project)
    assert "forced KILL" in (capture / "watchers-stopped.txt").read_text()
    assert (capture / "SUMMARY.md").is_file()
    assert (capture / "system-profiler-after.txt").is_file()


def test_early_whatcable_watcher_exit_marks_observation_incomplete(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none", watch_early_exit=True)

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert "OBSERVATION_INCOMPLETE" in (capture / "SUMMARY.md").read_text()
    assert "WhatCable watcher ended before cleanup" in (
        capture / "observer-errors.txt"
    ).read_text()


def test_unified_log_failure_marks_observation_incomplete(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="none", log_fail=True)

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert "OBSERVATION_INCOMPLETE" in (capture / "SUMMARY.md").read_text()
    assert "unified log query failed with status 9" in (
        capture / "observer-errors.txt"
    ).read_text()


def test_aborted_observation_never_gets_a_normal_result_classification(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, mode="persistent", main_sleep_fail=True)

    completed = _run_staged(project, env)

    assert completed.returncode == 143
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "- Classification: **RUN_ABORTED**" in summary
    assert (
        "- Classification: **TARGET_1CBE_0005_PRESENT_AFTER_WINDOW**"
        not in summary
    )
