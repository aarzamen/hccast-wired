from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture-macos-setr-once.sh"
EXACT_SETR_HEX = (
    "00 00 00 14 00 00 00 00 52 54 45 53 00 00 00 01 00 00 00 00"
)


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip())
    path.chmod(0o755)


def test_setr_capture_script_exists_is_executable_and_has_valid_syntax() -> None:
    assert CAPTURE_SCRIPT.is_file()
    assert os.access(CAPTURE_SCRIPT, os.X_OK)
    completed = subprocess.run(
        ["bash", "-n", str(CAPTURE_SCRIPT)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_setr_capture_script_contains_only_the_dedicated_bounded_probe() -> None:
    script = CAPTURE_SCRIPT.read_text()
    required = (
        "LIBUSB_DEBUG=4",
        ".venv/bin/hccast-wired",
        "host-setr-once",
        "--vendor-id 0x1cbe",
        "--product-id 0x0005",
        "--interface 0",
        '"${WAIT_SECONDS:-120}"',
        '"${POLL_INTERVAL:-0.01}"',
        "--response-timeout-ms 500",
        "--raw-output",
        "--try-claim-with-kernel-driver",
        EXACT_SETR_HEX,
        "adequately charged",
        "externally powered through its POWER port",
        "fully OFF",
        "DATA connection is disconnected",
        "normal 5+ second power-button hold",
    )
    for token in required:
        assert token in script

    for forbidden in (
        "host-handshake",
        "host-stream",
        "gadget-stream",
        "--detach-kernel",
        "--allow-configuration-activation",
        "--set-configuration",
        "curl ",
        "wget ",
        "http://",
        "https://",
    ):
        assert forbidden not in script


def _stage_fake_project(
    tmp_path: Path,
    *,
    classification: str = "VALID_SETV",
    cli_status: int = 0,
    raw_response: bytes = b"\x00\x00\x00\x14\x00\x00\x00\x01VTES\x00\x00\x00\x00ABCD",
    stubborn_watcher: bool = False,
    xxd_fail: bool = False,
    xxd_partial: bool = False,
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
    cli_args = tmp_path / "cli-args.txt"
    cli_calls = tmp_path / "cli-calls.txt"
    profiler_calls = tmp_path / "system-profiler-calls.txt"
    log_args = tmp_path / "log-args.txt"
    ioreg_args = tmp_path / "ioreg-args.txt"
    xxd_calls = tmp_path / "xxd-calls.txt"
    raw_fixture = tmp_path / "raw-response-fixture.bin"
    raw_fixture.write_bytes(raw_response)

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
        printf 'kernel: USB fake attach event for 1cbe:0005\n'
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
        fake_bin / "xxd",
        r"""
        #!/usr/bin/env bash
        printf 'call\n' >> "$XXD_CALLS_FILE"
        if [[ "${XXD_FAIL:-0}" == "1" ]]; then
          if [[ "${XXD_PARTIAL:-0}" == "1" ]]; then
            printf '00000000: 72 61 77  partial\n'
          fi
          printf 'fake xxd render failure\n' >&2
          exit 9
        fi
        exec /usr/bin/xxd "$@"
        """,
    )
    _write_executable(
        venv_bin / "hccast-wired",
        r"""
        #!/usr/bin/env bash
        printf 'call\n' >> "$CLI_CALLS_FILE"
        printf 'LIBUSB_DEBUG=%s %s\n' "${LIBUSB_DEBUG:-}" "$*" > "$CLI_ARGS_FILE"

        raw_output=''
        previous=''
        for argument in "$@"; do
          if [[ "$previous" == "--raw-output" ]]; then
            raw_output="$argument"
          fi
          previous="$argument"
        done
        if [[ -z "$raw_output" ]]; then
          printf 'missing --raw-output\n' >&2
          exit 65
        fi
        cp "$RAW_FIXTURE" "$raw_output"
        cat <<EOF
{
  "classification": "$CLI_CLASSIFICATION",
  "outbound_hex": "0000001400000000525445530000000100000000",
  "raw_output": "$raw_output",
  "response_bytes": $(wc -c < "$raw_output" | tr -d ' '),
  "write_error": null,
  "read_error": null,
  "parse_errors": []
}
EOF
        exit "$CLI_STATUS"
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "WATCH_STOP_FILE": str(watch_stop_file),
            "CLI_ARGS_FILE": str(cli_args),
            "CLI_CALLS_FILE": str(cli_calls),
            "PROFILER_CALLS": str(profiler_calls),
            "LOG_ARGS_FILE": str(log_args),
            "IOREG_ARGS_FILE": str(ioreg_args),
            "XXD_CALLS_FILE": str(xxd_calls),
            "XXD_FAIL": "1" if xxd_fail else "0",
            "XXD_PARTIAL": "1" if xxd_partial else "0",
            "RAW_FIXTURE": str(raw_fixture),
            "CLI_CLASSIFICATION": classification,
            "CLI_STATUS": str(cli_status),
            "STUBBORN_WATCHER": "1" if stubborn_watcher else "0",
            "WAIT_SECONDS": "0.2",
            "POLL_INTERVAL": "0.01",
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
    captures = list((project / "logs" / "whatcable").glob("*-host-setr-once"))
    assert len(captures) == 1
    return captures[0]


def test_fake_valid_setv_records_exact_command_payload_raw_evidence_and_cleanup(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(tmp_path)

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    assert "command not found" not in completed.stderr
    capture = _only_capture(project)
    actual_command = Path(env["CLI_ARGS_FILE"]).read_text().strip()
    expected_raw = capture / "raw-response.bin"
    assert actual_command == (
        "LIBUSB_DEBUG=4 -vv host-setr-once --vendor-id 0x1cbe "
        "--product-id 0x0005 --interface 0 --wait-seconds 0.2 "
        "--poll-interval 0.01 --response-timeout-ms 500 "
        f"--raw-output {expected_raw} --try-claim-with-kernel-driver"
    )
    assert Path(env["CLI_CALLS_FILE"]).read_text().splitlines() == ["call"]
    assert (capture / "setr-outbound.hex").read_text().strip() == EXACT_SETR_HEX
    assert expected_raw.read_bytes() == Path(env["RAW_FIXTURE"]).read_bytes()
    assert "00 00 00 14" in (capture / "raw-response.hex").read_text()
    assert '"classification": "VALID_SETV"' in (capture / "probe-stdout.json").read_text()
    summary = (capture / "SUMMARY.md").read_text()
    result = (capture / "probe-result.txt").read_text()
    assert "VALID_SETV" in summary
    assert f"- Exact outbound bytes: {EXACT_SETR_HEX}" in summary
    assert "classification: VALID_SETV" in result
    assert "exactly one SETR" in summary
    assert "500 ms" in summary
    assert "does not establish general display compatibility" in summary
    assert (capture / "whatcable-after.json").is_file()
    assert (capture / "system-profiler-after.txt").read_text() == "FAKE_HOST_USB_TREE\n"
    assert (capture / "ioreg-after.txt").is_file()
    assert "IOUSBHostInterface" in (capture / "ioreg-transient-target.txt").read_text()
    assert "USB fake attach event for 1cbe:0005" in (
        capture / "usb-key-events.txt"
    ).read_text()
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"
    assert (capture / "watchers-stopped.txt").is_file()


@pytest.mark.parametrize(
    ("classification", "raw_response"),
    [
        ("PARTIAL_HCCAST_RESPONSE", b"\x00\x00\x00\x40partial"),
        ("RAW_NON_HCCAST_RESPONSE", b"not-hccast"),
        ("SETR_WRITE_OK_NO_RESPONSE", b""),
        ("SETR_WRITE_FAILED", b""),
    ],
)
def test_fake_probe_preserves_each_classification_and_raw_bytes(
    tmp_path: Path,
    classification: str,
    raw_response: bytes,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        classification=classification,
        raw_response=raw_response,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert classification in (capture / "SUMMARY.md").read_text()
    assert classification in (capture / "probe-result.txt").read_text()
    assert (capture / "raw-response.bin").read_bytes() == raw_response
    raw_hex = (capture / "raw-response.hex").read_text()
    if raw_response:
        assert raw_hex.strip()
    else:
        assert raw_hex == ""


def test_cli_error_finalizes_evidence_and_does_not_invent_probe_classification(
    tmp_path: Path,
) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        classification="NOT_EMITTED",
        cli_status=1,
        raw_response=b"already-preserved",
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 1
    capture = _only_capture(project)
    assert "COMMAND_ERROR" in (capture / "SUMMARY.md").read_text()
    assert "classification: COMMAND_ERROR" in (capture / "probe-result.txt").read_text()
    assert (capture / "raw-response.bin").read_bytes() == b"already-preserved"
    assert (capture / "whatcable-after.json").is_file()
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"


def test_nonempty_raw_hex_render_failure_is_visible_and_nonzero(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        raw_response=b"raw-response-survives",
        xxd_fail=True,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 74
    capture = _only_capture(project)
    assert (capture / "raw-response.bin").read_bytes() == b"raw-response-survives"
    assert (capture / "raw-response.hex").read_text() == ""
    assert "fake xxd render failure" in (
        capture / "raw-response-xxd.stderr.txt"
    ).read_text()
    assert "returned success" not in (
        capture / "raw-response-xxd.stderr.txt"
    ).read_text()
    assert "RAW_HEX_RENDER_FAILED" in (capture / "SUMMARY.md").read_text()
    assert "evidence status: RAW_HEX_RENDER_FAILED" in (
        capture / "probe-result.txt"
    ).read_text()
    assert "raw hex renderer exit status: 9" in (
        capture / "probe-result.txt"
    ).read_text()
    assert (capture / "whatcable-after.json").is_file()
    assert Path(env["WATCH_STOP_FILE"]).read_text() == "stopped"


def test_partial_hex_output_does_not_mask_renderer_failure(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        raw_response=b"raw-response-survives",
        xxd_fail=True,
        xxd_partial=True,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 74
    capture = _only_capture(project)
    assert (capture / "raw-response.bin").read_bytes() == b"raw-response-survives"
    assert "partial" in (capture / "raw-response.hex").read_text()
    result = (capture / "probe-result.txt").read_text()
    assert "evidence status: RAW_HEX_RENDER_FAILED" in result
    assert "raw hex renderer exit status: 9" in result


def test_empty_raw_has_valid_empty_hex_without_renderer_dependency(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        classification="SETR_WRITE_OK_NO_RESPONSE",
        raw_response=b"",
        xxd_fail=True,
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 0, completed.stderr
    capture = _only_capture(project)
    assert (capture / "raw-response.bin").read_bytes() == b""
    assert (capture / "raw-response.hex").read_text() == ""
    assert "evidence status: OK" in (capture / "probe-result.txt").read_text()
    assert not Path(env["XXD_CALLS_FILE"]).exists()


def test_write_failed_classification_survives_distinct_cli_status(tmp_path: Path) -> None:
    project, env = _stage_fake_project(
        tmp_path,
        classification="SETR_WRITE_FAILED",
        cli_status=3,
        raw_response=b"",
    )

    completed = _run_staged(project, env)

    assert completed.returncode == 3
    capture = _only_capture(project)
    assert "SETR_WRITE_FAILED" in (capture / "SUMMARY.md").read_text()
    assert "classification: SETR_WRITE_FAILED" in (
        capture / "probe-result.txt"
    ).read_text()


def test_stubborn_watcher_is_killed_without_blocking_finalization(tmp_path: Path) -> None:
    project, env = _stage_fake_project(tmp_path, stubborn_watcher=True)

    started = time.monotonic()
    completed = _run_staged(project, env)
    elapsed = time.monotonic() - started

    assert completed.returncode == 0, completed.stderr
    assert elapsed < 5
    capture = _only_capture(project)
    assert "forced KILL" in (capture / "watchers-stopped.txt").read_text()
    assert (capture / "system-profiler-after.txt").is_file()
