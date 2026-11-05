"""Command-line contract checks that never open USB or touch a UDC."""

from __future__ import annotations

import importlib.metadata
import re

import pytest

import hccast_wired
from hccast_wired import cli


def test_version_option_reports_the_single_driver_version_source(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--version"])

    assert raised.value.code == 0
    out = capsys.readouterr().out.strip()
    assert out == f"hccast-wired {hccast_wired.__version__}"
    assert re.fullmatch(r"hccast-wired [0-9]+\.[0-9]+\.[0-9]+", out)


def test_installed_metadata_matches_the_package_version_source() -> None:
    try:
        installed = importlib.metadata.version("hccast-wired")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("hccast-wired is not installed in this environment")
    assert installed == hccast_wired.__version__


def test_help_mentions_version_and_lists_every_command(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        cli.main(["--help"])

    assert raised.value.code == 0
    out = capsys.readouterr().out
    assert "--version" in out
    for command in (
        "probe-host",
        "host-claim",
        "host-setr-once",
        "host-handshake",
        "host-stream",
        "gadget-handshake",
        "gadget-stream",
        "gadget-stop",
    ):
        assert command in out
