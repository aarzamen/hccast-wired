from __future__ import annotations

import pytest

from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase, RuntimeStatus


def test_defaults_are_safe_and_stopped() -> None:
    config = LiveConfig()

    assert config.mode is DesiredMode.STOPPED
    assert (config.width, config.height) == (640, 1136)
    assert config.fps == 10
    assert config.bitrate_kbps == 4000
    assert config.kiosk_url == "http://127.0.0.1:3000"
    assert config.source_user == "hccast"


@pytest.mark.parametrize("fps", [0, 31, True, 10.5])
def test_invalid_fps_is_rejected(fps: object) -> None:
    with pytest.raises(ValueError, match="fps"):
        LiveConfig.from_mapping({"fps": fps})


@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "javascript:alert(1)", "http://u:p@example.test", "http:///x"],
)
def test_unsafe_kiosk_url_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="kiosk_url"):
        LiveConfig.from_mapping({"kiosk_url": url})


@pytest.mark.parametrize("url", ["http://example.test:", "https://[::1]:"])
def test_kiosk_url_with_empty_explicit_port_is_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="kiosk_url"):
        LiveConfig.from_mapping({"kiosk_url": url})


def test_bracketed_ipv6_kiosk_url_without_port_is_accepted() -> None:
    url = "https://[::1]/status"

    assert LiveConfig.from_mapping({"kiosk_url": url}).kiosk_url == url


def test_runtime_status_keeps_desired_mode_separate_from_phase() -> None:
    status = RuntimeStatus(desired_mode=DesiredMode.DESKTOP, phase=RuntimePhase.RETRYING)

    assert status.to_mapping()["desired_mode"] == "desktop"
    assert status.to_mapping()["phase"] == "retrying"


def test_config_mapping_includes_all_fixed_operational_constraints() -> None:
    config = LiveConfig()

    assert config.to_mapping() == {
        "schema_version": 1,
        "mode": "stopped",
        "kiosk_url": "http://127.0.0.1:3000",
        "fps": 10,
        "bitrate_kbps": 4000,
        "source_user": "hccast",
        "novnc_enabled": True,
        "width": 640,
        "height": 1136,
        "display_number": 99,
        "controller_host": "127.0.0.1",
        "controller_port": 8765,
        "novnc_host": "127.0.0.1",
        "novnc_port": 6080,
        "run_retention_count": 20,
        "run_retention_bytes": 200 * 1024 * 1024,
    }


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"width": 641}, "width"),
        ({"height": 1137}, "height"),
        ({"display_number": 98}, "display_number"),
        ({"controller_host": "0.0.0.0"}, "controller_host"),
        ({"novnc_host": "0.0.0.0"}, "novnc_host"),
        ({"schema_version": 2}, "schema_version"),
    ],
)
def test_fixed_constraints_cannot_be_changed(changes: dict[str, object], field: str) -> None:
    with pytest.raises(ValueError, match=field):
        LiveConfig.from_mapping(changes)


@pytest.mark.parametrize("source_user", ["", "two words", "AUser", "user!"])
def test_invalid_source_user_is_rejected(source_user: str) -> None:
    with pytest.raises(ValueError, match="source_user"):
        LiveConfig.from_mapping({"source_user": source_user})


def test_with_updates_returns_a_validated_replacement() -> None:
    original = LiveConfig()
    updated = original.with_updates(mode="kiosk", kiosk_url="https://example.test:8443/path")

    assert updated.mode is DesiredMode.KIOSK
    assert updated.kiosk_url == "https://example.test:8443/path"
    assert original.mode is DesiredMode.STOPPED


def test_runtime_status_mapping_is_json_safe() -> None:
    status = RuntimeStatus(
        desired_mode=DesiredMode.KIOSK,
        phase=RuntimePhase.STREAMING,
        retry_count=2,
        next_retry_at="2026-07-21T12:00:00Z",
        attempt_started_at="2026-07-21T11:59:00Z",
        product="HCT-AT01",
        version="2505161526",
        last_error=None,
    )

    assert status.to_mapping() == {
        "desired_mode": "kiosk",
        "phase": "streaming",
        "retry_count": 2,
        "next_retry_at": "2026-07-21T12:00:00Z",
        "attempt_started_at": "2026-07-21T11:59:00Z",
        "product": "HCT-AT01",
        "version": "2505161526",
        "last_error": None,
    }
