from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture-macos-host-claim.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip())
    path.chmod(0o755)


def test_claim_capture_script_exists_is_executable_and_has_valid_syntax() -> None:
    assert CAPTURE_SCRIPT.is_file()
    assert os.access(CAPTURE_SCRIPT, os.X_OK)
    completed = subprocess.run(
        ["bash", "-n", str(CAPTURE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_claim_capture_script_contains_exact_safe_diagnostic() -> None:
    script = CAPTURE_SCRIPT.read_text()
    required = (
        "LIBUSB_DEBUG=4",
        ".venv/bin/hccast-wired",
        "host-claim",
        "--vendor-id 0x1cbe",
        "--product-id 0x0005",
        "--interface 0",
        '"${WAIT_SECONDS:-120}"',
        '"${POLL_INTERVAL:-0.01}"',
        '"${HOLD_SECONDS:-0}"',
        "--hold-seconds",
        "--try-claim-with-kernel-driver",
    )
    for token in required:
        assert token in script
    assert "reset|scsi|storage|mass|disk|msc" in script
    assert "normal 5+ second power-button hold" in script
    assert "Make one boot\nattempt" in script

    for forbidden in (
        "host-handshake",
        "host-stream",
        "--detach-kernel",
        "sudo",
        "gadget-stream",
        "firmware",
        "http://",
        "https://",
    ):
        assert forbidden not in script


def _stage_fake_project(
    tmp_path: Path,
    *,
    claim_status: int,
    claim_mode: str = "claim",
    kernel_log_mode: str = "attach",
    stubborn_watcher: bool = False,
    split_ioreg_records: bool = False,
    ioreg_target_visible: bool = True,
) -> tuple[Path, dict[str, str]]:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "fake-bin"
    venv_bin = project / ".venv" / "bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()
    venv_bin.mkdir(parents=True)
    shutil.copy2(CAPTURE_SCRIPT, scripts / CAPTURE_SCRIPT.name)

    watch_stop_file = tmp_path / "whatcable-watch-stopped.txt"
    profiler_calls = tmp_path / "system-profiler-calls.txt"
    cli_args = tmp_path / "cli-args.txt"
    log_args = tmp_path / "log-args.txt"
    ioreg_args = tmp_path / "ioreg-args.txt"

    _write_executable(
        fake_bin / "uname",
        r"""
        #!/usr/bin/env bash
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
        printf 'ProductVersion:\t26.5.1\n'
        """,
    )
    _write_executable(
        fake_bin / "whatcable",
        r"""
        #!/usr/bin/env bash
        case "${1:-}" in
          --json) printf '{"fake":"snapshot"}\n' ;;
          --raw) printf 'FAKE_RAW_CABLE_STATE\n' ;;
          --version) printf 'WhatCable fake 1.0\n' ;;
          --watch)
            if [[ "${STUBBORN_WATCHER:-0}" == "1" ]]; then
              trap '' TERM
            else
              trap 'printf stopped > "$WATCH_STOP_FILE"; exit 0' TERM INT
            fi
            while :; do
              printf '{"fake":"watch"}\n'
              sleep 0.02
            done
            ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "system_profiler",
        r"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> "$PROFILER_CALLS"
        case "${1:-}" in
          -listDataTypes) printf 'SPUSBHostDataType\n' ;;
          SPUSBHostDataType) printf 'FAKE_HOST_USB_TREE\n' ;;
          SPUSBDataType) printf 'LEGACY_SHOULD_NOT_BE_USED\n' ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "ioreg",
        r"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" >> "$IOREG_ARGS_FILE"
        if [[ "${IOREG_TARGET_VISIBLE:-1}" == "0" ]]; then
          cat <<'EOF'
+-o UnrelatedInterface  <class IOUSBHostInterface>
  | {"idVendor" = 12, "idProduct" = 99}
EOF
          exit 0
        fi
        if [[ "${SPLIT_IOREG_RECORDS:-0}" == "1" ]]; then
          cat <<'EOF'
+-o FirstInterface  <class IOUSBHostInterface>
  | {"idVendor" = 7358, "idProduct" = 99}
+-o SecondInterface  <class IOUSBHostInterface>
  | {"idVendor" = 12, "idProduct" = 5}
EOF
          exit 0
        fi
        cat <<'EOF'
+-o IOUSBHostInterface@0  <class IOUSBHostInterface>
  | {"idVendor" = 7358, "idProduct" = 5}
  +-o FakeInterfaceChild  <class AppleUSBHostInterfaceUserClient>
EOF
        """,
    )
    _write_executable(
        fake_bin / "log",
        r"""
        #!/usr/bin/env bash
        printf '%s\n' "$*" > "$LOG_ARGS_FILE"
        previous=''
        for argument in "$@"; do
          if [[ "$previous" == "--start" || "$previous" == "--end" ]]; then
            if [[ ! "$argument" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\ [0-9]{2}:[0-9]{2}:[0-9]{2}$ ]]; then
              printf 'invalid log timestamp: %s\n' "$argument" >&2
              exit 65
            fi
          fi
          previous="$argument"
        done
        case "${KERNEL_LOG_MODE:-attach}" in
          attach)
            printf 'kernel: USB fake attach event for 1cbe:0005\n'
            ;;
          repeated-enumeration-failures)
            printf '%s\n' \
              'kernel: AppleUSBHostPort::setAddress: failed to set device address' \
              'kernel: AppleUSBHostPort::createDevice: failed to create device' \
              'kernel: AppleUSBHostPort::setAddress: failed to set device address'
            ;;
          persistent-enumeration-failures)
            printf 'kernel: persistent enumeration failures on USB port\n'
            ;;
          single-enumeration-failure)
            printf 'kernel: AppleUSBHostPort::setAddress: failed to set device address\n'
            ;;
          *) exit 66 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "rg",
        r"""
        #!/usr/bin/env bash
        for argument in "$@"; do
          if [[ "$argument" == -* && "$argument" == *E* ]]; then
            printf 'fake ripgrep rejects grep-only -E flag: %s\n' "$argument" >&2
            exit 64
          fi
        done
        exec /usr/bin/grep -E "$@"
        """,
    )
    _write_executable(
        venv_bin / "hccast-wired",
        r"""
        #!/usr/bin/env bash
        printf 'LIBUSB_DEBUG=%s %s\n' "${LIBUSB_DEBUG:-}" "$*" > "$CLI_ARGS_FILE"
        sleep 0.05
        if [[ "$CLAIM_STATUS" == "0" ]]; then
          printf '%s\n' \
            'USB interface claim succeeded; releasing after application payload silence.'
          exit 0
        fi
        case "$CLAIM_MODE" in
          claim)
            printf '%s\n' \
              'ERROR: cannot claim interface 0; non-detaching claim was attempted: fake busy' >&2
            ;;
          missing) printf 'ERROR: USB device 1cbe:0005 not found\n' >&2 ;;
          other) printf 'ERROR: unexpected descriptor failure\n' >&2 ;;
          *) exit 66 ;;
        esac
        exit "$CLAIM_STATUS"
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "WATCH_STOP_FILE": str(watch_stop_file),
            "PROFILER_CALLS": str(profiler_calls),
            "CLI_ARGS_FILE": str(cli_args),
            "LOG_ARGS_FILE": str(log_args),
            "IOREG_ARGS_FILE": str(ioreg_args),
            "CLAIM_STATUS": str(claim_status),
            "CLAIM_MODE": claim_mode,
            "KERNEL_LOG_MODE": kernel_log_mode,
            "STUBBORN_WATCHER": "1" if stubborn_watcher else "0",
            "SPLIT_IOREG_RECORDS": "1" if split_ioreg_records else "0",
            "IOREG_TARGET_VISIBLE": "1" if ioreg_target_visible else "0",
            "WAIT_SECONDS": "0.2",
            "POLL_INTERVAL": "0.01",
            "HOLD_SECONDS": "2",
            "IOREG_POLL_INTERVAL": "0.01",
            "WATCHER_STOP_ATTEMPTS": "2",
            "WATCHER_STOP_INTERVAL": "0.01",
        }
    )
    return project, env


def _run_staged(project: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(project / "scripts" / CAPTURE_SCRIPT.name)],
        cwd=project,
        env=env,
        input="\n",
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )


def _only_capture(project: Path) -> Path:
    captures = list((project / "logs" / "whatcable").glob("*-host-claim"))
    assert len(captures) == 1
    return captures[0]


def test_fake_success_captures_evidence_classifies_and_cleans_watchers(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, claim_status=0)

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    assert "command not found" not in completed.stderr
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "CLAIM_SUCCEEDED_NO_IO" in summary
    assert "no HCCAST/application bulk-endpoint payload bytes were read or written" in summary
    assert "no session was constructed" in summary
    assert "no configuration activation" in summary
    assert "no kernel-driver detachment" in summary
    assert "enumeration, descriptors, and control machinery" in summary
    assert "proves only userspace interface ownership" in summary
    assert (capture / "whatcable-after.json").is_file()
    assert (capture / "system-profiler-after.txt").read_text() == "FAKE_HOST_USB_TREE\n"
    assert (capture / "ioreg-after.txt").is_file()
    transient_ioreg = (capture / "ioreg-transient-target.txt").read_text()
    assert "IOUSBHostInterface" in transient_ioreg
    assert "FakeInterfaceChild" in transient_ioreg
    assert (capture / "kernel-log.txt").is_file()
    assert "USB fake attach event for 1cbe:0005" in (
        capture / "usb-key-events.txt"
    ).read_text()
    assert (capture / "watchers-stopped.txt").is_file()
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"
    command = (capture / "claim-command.txt").read_text()
    assert "LIBUSB_DEBUG=4" in command
    assert "host-claim" in command
    assert "--try-claim-with-kernel-driver" in command
    assert "--hold-seconds 2" in command
    assert "exit status: 0" in (capture / "claim-result.txt").read_text()
    actual_command = Path(env["CLI_ARGS_FILE"]).read_text().strip()
    assert actual_command == (
        "LIBUSB_DEBUG=4 -vv host-claim --vendor-id 0x1cbe --product-id 0x0005 "
        "--interface 0 --wait-seconds 0.2 --poll-interval 0.01 "
        "--hold-seconds 2 "
        "--try-claim-with-kernel-driver"
    )
    log_args = Path(env["LOG_ARGS_FILE"]).read_text().strip()
    assert log_args.startswith("show --start ")
    assert " --end " in log_args
    assert " --style compact --predicate " in log_args
    assert "T" not in log_args.split(" --end ", maxsplit=1)[0]
    assert 'process == "kernel" AND (' in log_args
    assert 'process == "kernel" OR subsystem' not in log_args
    for term in ("SCSI", "storage", "mass", "disk", "MSC"):
        assert f'eventMessage CONTAINS[c] "{term}"' in log_args
    result = (capture / "claim-result.txt").read_text()
    assert "log query start local:" in result
    assert "log query end local:" in result
    assert "requested hold seconds: 2" in result
    assert "Requested claim hold seconds: 2" in summary
    assert "does not establish that the device remained attached" in summary
    ioreg_calls = Path(env["IOREG_ARGS_FILE"]).read_text().splitlines()
    assert "-r -c IOUSBHostInterface -l -w0" in ioreg_calls
    assert "-p IOUSB -r -c IOUSBHostInterface -l -w0" not in ioreg_calls


def test_fake_claim_failure_collects_after_state_and_preserves_status(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, claim_status=7)

    completed = _run_staged(project, env)

    assert completed.returncode == 7
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    assert "CLAIM_FAILED_NONDETACHING" in summary
    assert "no HCCAST/application bulk-endpoint payload bytes were read or written" in summary
    assert (capture / "whatcable-after-raw.txt").is_file()
    assert (capture / "system-profiler-after.txt").is_file()
    assert (capture / "ioreg-after.txt").is_file()
    assert "exit status: 7" in (capture / "claim-result.txt").read_text()
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"


def test_fake_target_not_observed_classification_preserves_status(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=3,
        claim_mode="missing",
        ioreg_target_visible=False,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    assert "TARGET_NOT_OBSERVED" in (capture / "SUMMARY.md").read_text()
    assert "exit status: 3" in (capture / "claim-result.txt").read_text()


def test_repeated_kernel_enumeration_failures_are_distinct_from_target_not_observed(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=3,
        claim_mode="missing",
        kernel_log_mode="repeated-enumeration-failures",
        ioreg_target_visible=False,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    summary = (capture / "SUMMARY.md").read_text()
    result = (capture / "claim-result.txt").read_text()
    assert "USB_ENUMERATION_FAILED_BEFORE_TARGET_OBSERVED" in summary
    assert "classification: USB_ENUMERATION_FAILED_BEFORE_TARGET_OBSERVED" in result
    assert "TARGET_NOT_OBSERVED" not in summary


def test_explicit_persistent_enumeration_failure_is_classified_distinctly(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=3,
        claim_mode="missing",
        kernel_log_mode="persistent-enumeration-failures",
        ioreg_target_visible=False,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    assert "USB_ENUMERATION_FAILED_BEFORE_TARGET_OBSERVED" in (
        capture / "SUMMARY.md"
    ).read_text()


def test_single_kernel_enumeration_failure_remains_target_not_observed(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=3,
        claim_mode="missing",
        kernel_log_mode="single-enumeration-failure",
        ioreg_target_visible=False,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    assert "TARGET_NOT_OBSERVED" in (capture / "SUMMARY.md").read_text()


def test_ioreg_target_with_driver_missing_is_other_failure_and_explained(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=3,
        claim_mode="missing",
        ioreg_target_visible=True,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    explanation = (
        "macOS observed the target but the claim tool did not acquire/find it"
    )
    summary = (capture / "SUMMARY.md").read_text()
    result = (capture / "claim-result.txt").read_text()
    assert "OTHER_FAILURE" in summary
    assert "classification: OTHER_FAILURE" in result
    assert explanation in summary
    assert explanation in result
    assert "exit status: 3" in result
    assert "no HCCAST/application bulk-endpoint payload bytes were read or written" in summary


def test_fake_other_failure_classification_preserves_status(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, claim_status=9, claim_mode="other")

    completed = _run_staged(project, env)

    assert completed.returncode == 9
    capture = _only_capture(project)
    assert "OTHER_FAILURE" in (capture / "SUMMARY.md").read_text()
    assert "exit status: 9" in (capture / "claim-result.txt").read_text()


def test_stubborn_watcher_is_killed_without_blocking_finalization(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=7,
        stubborn_watcher=True,
    )

    started = time.monotonic()
    completed = _run_staged(project, env)
    elapsed = time.monotonic() - started

    assert completed.returncode == 7
    assert elapsed < 5
    capture = _only_capture(project)
    assert (capture / "system-profiler-after.txt").is_file()
    assert "CLAIM_FAILED_NONDETACHING" in (capture / "SUMMARY.md").read_text()
    assert "exit status: 7" in (capture / "claim-result.txt").read_text()
    assert "forced KILL" in (capture / "watchers-stopped.txt").read_text()


def test_transient_match_requires_vendor_and_product_in_same_service(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        claim_status=0,
        split_ioreg_records=True,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert (capture / "ioreg-transient-target.txt").read_text() == ""


def test_fake_capture_prefers_advertised_host_system_profiler_type(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, claim_status=0)

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    profiler_calls = Path(env["PROFILER_CALLS"]).read_text()
    assert "SPUSBHostDataType" in profiler_calls
    assert "SPUSBDataType" not in profiler_calls
