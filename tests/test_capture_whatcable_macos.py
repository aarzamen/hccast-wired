from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = PROJECT_ROOT / "scripts" / "capture-whatcable-macos.sh"


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(textwrap.dedent(contents).lstrip())
    path.chmod(0o755)


def test_capture_uses_advertised_usb_system_profiler_data_type(tmp_path: Path) -> None:
    project = tmp_path / "project"
    scripts = project / "scripts"
    fake_bin = tmp_path / "fake-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()

    copied_script = scripts / CAPTURE_SCRIPT.name
    shutil.copy2(CAPTURE_SCRIPT, copied_script)

    _write_executable(
        fake_bin / "uname",
        r"""
        #!/usr/bin/env bash
        if [[ "${1:-}" == "-s" ]]; then
          printf 'Darwin\n'
        else
          printf 'Darwin test-host 26.5.1\n'
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
          --json) printf '{}\n' ;;
          --raw) printf 'RAW_CABLE_STATE\n' ;;
          --watch) printf '{"watch": true}\n' ;;
          --version) printf 'WhatCable test double\n' ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "system_profiler",
        r"""
        #!/usr/bin/env bash
        case "${1:-}" in
          -listDataTypes) printf 'SPUSBHostDataType\n' ;;
          SPUSBHostDataType) printf 'HOST_USB_TREE\n' ;;
          SPUSBDataType) : ;;
          *) exit 64 ;;
        esac
        """,
    )
    _write_executable(
        fake_bin / "ioreg",
        r"""
        #!/usr/bin/env bash
        printf 'FAKE_IOREG_TREE\n'
        """,
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["WATCH_SECONDS"] = "0"
    completed = subprocess.run(
        ["bash", str(copied_script), "test-topology"],
        cwd=project,
        env=env,
        input="\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    capture_dirs = list((project / "logs" / "whatcable").iterdir())
    assert len(capture_dirs) == 1
    capture_dir = capture_dirs[0]
    snapshots = {
        phase: (capture_dir / f"system-profiler-{phase}.txt").read_text()
        for phase in ("before", "after")
    }
    assert snapshots == {
        "before": "HOST_USB_TREE\n",
        "after": "HOST_USB_TREE\n",
    }
