"""UI decisions without GTK or hardware; fixtures use synthetic identities."""

import copy
import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.ui import presenter


PROFILE = {
    "source_width": 1280, "source_height": 720, "content_fps": 15,
    "wire_width": 1280, "wire_height": 720, "wire_fps": 30,
    "bitrate_kbps": 3500, "scale": 1.0, "rotation": 0,
}


def session(**updates):
    value = {
        "api_version": 1, "status": "stopped", "message": "Ready",
        "receiver": {"name": "Example display", "transport": "wireless",
                     "verification": "experimental"},
        "candidates": [{"id": "peer-example", "name": "Example display",
                        "manufacturer": "Example", "transport": "wireless"}],
        "profile": copy.deepcopy(PROFILE), "presets": {},
        "health": {"temperature_c": None, "cpu_percent": None, "throttled_bits": None},
        "trial": None, "features": ["discover", "start", "stop", "select",
                                    "apply_profile", "confirm_profile", "revert_profile"],
        "recovery": {"ssh_available": True, "lan_enrolled": False, "message": "Ethernet"},
    }
    value.update(updates)
    return value


def ready(**updates):
    view = presenter.Presenter(clock=lambda: 100.0)
    view.update(session(**updates))
    return view


def test_service_loss_revokes_stale_actions_and_allows_reconnect_state():
    view = ready(status="streaming")
    assert view.availability("stop").enabled
    view.disconnect("Controller disappeared")
    assert not view.availability("stop").enabled
    assert not view.connected
    assert view.error == "Controller disappeared"
    view.update(session())
    assert view.connected and not view.error
    assert view.availability("start").enabled


def test_unsupported_and_preview_actions_never_reach_a_command():
    view = ready(feature_reasons={"calibrate": "Calibration is not available yet"})
    assert "Calibration" in view.availability("calibrate").reason
    with pytest.raises(ValueError):
        view.begin("calibrate")
    view = presenter.Presenter(preview=True)
    view.update(session())
    with pytest.raises(ValueError, match="Preview"):
        view.begin("start")


def test_duplicate_mutations_block_but_trial_rollback_stays_available():
    view = ready(trial={"kind": "profile", "seconds_remaining": 12,
                       "message": "Confirm the display"})
    view.begin("confirm_profile")
    assert not view.availability("confirm_profile").enabled
    assert view.availability("revert_profile").enabled
    view.finish("confirm_profile", error="Try again")
    assert view.availability("confirm_profile").enabled
    assert view.error == "Try again"


def test_profile_trial_blocks_second_profile_and_requires_live_confirmation():
    view = ready()
    assert not view.availability("confirm_profile").enabled
    view.update(session(trial={"kind": "profile", "seconds_remaining": 12}))
    assert not view.availability("apply_profile").enabled
    assert view.trial_remaining(now=105.0) == 7
    assert view.trial_remaining(now=114.0) == 0
    assert view.availability("confirm_profile", now=114.0).enabled is False
    assert view.availability("revert_profile", now=114.0).enabled


def test_missing_health_is_unknown_and_zero_is_a_real_reading():
    view = ready()
    assert "Unknown" in view.health_text()
    view.update(session(health={"temperature_c": 0, "cpu_percent": 0, "throttled_bits": 0}))
    assert "0 °C" in view.health_text()
    assert "0%" in view.health_text()
    assert "No flags" in view.health_text()


def test_content_and_wire_rates_stay_distinct_and_native_pixels_unknown():
    summary = presenter.profile_summary(PROFILE)
    assert "15 fps" in summary and "30 Hz" in summary
    assert "1280 × 720" in summary
    assert ready().native_resolution_text() == "Native panel resolution: unknown"


def test_uncalibrated_presets_disabled_and_no_power_claim_invented():
    view = ready(presets={"battery_saver": {"available": False, "reason": "No meter results",
                                          "settings": None, "measurement": None}})
    preset = view.preset("battery_saver")
    assert not preset.enabled and preset.reason == "No meter results"
    assert "not measured" in preset.measurement.lower()
    with pytest.raises(ValueError):
        view.begin("apply_profile", {"preset": "battery_saver"})


def test_calibrated_preset_keeps_controller_settings_and_measurement():
    view = ready(presets={"recommended": {"available": True, "settings": PROFILE,
                                        "measurement": "Measured compute load; power unmeasured"}})
    preset = view.preset("recommended")
    assert preset.enabled
    assert preset.measurement == "Measured compute load; power unmeasured"
    assert view.begin("apply_profile", {"preset": "recommended"}) == (
        "apply_profile", {"preset": "recommended"})


def test_only_explicit_cpu_choices_can_be_sent():
    view = ready(features=["set_cpu_cap", "trial_clock"])
    assert not view.availability("set_cpu_cap").enabled
    view.update(session(features=["set_cpu_cap", "trial_clock"],
                        capabilities={"cpu_caps_mhz": [1800, 2400], "trial_clocks_mhz": []}))
    with pytest.raises(ValueError):
        view.begin("set_cpu_cap", {"mhz": 2700})
    assert view.begin("set_cpu_cap", {"mhz": 1800}) == ("set_cpu_cap", {"mhz": 1800})
    assert not view.availability("trial_clock").enabled


def test_candidate_selection_rejects_expired_and_usb_candidates():
    view = ready()
    with pytest.raises(ValueError):
        view.begin("select", {"candidate_id": "old-peer"})
    view.update(session(candidates=[{"id": "usb-peer", "name": "Unknown USB",
                                     "transport": "usb", "manufacturer": "Unknown"}]))
    with pytest.raises(ValueError):
        view.begin("select", {"candidate_id": "usb-peer"})


@pytest.mark.parametrize("key,value", [("content_fps", "NaN"), ("scale", "inf"),
                                      ("source_width", "0"), ("rotation", "45"),
                                      ("wire_fps", "29.97"), ("bitrate_kbps", True)])
def test_invalid_manual_profile_never_leaves_the_ui(key, value):
    fields = {**PROFILE, key: value}
    with pytest.raises(ValueError):
        presenter.parse_profile(fields)


def test_manual_profile_retains_all_fields_and_requires_complete_record():
    assert presenter.parse_profile({key: str(value) for key, value in PROFILE.items()}) == PROFILE
    with pytest.raises(ValueError):
        presenter.parse_profile({"source_width": 1280})
    view = ready()
    assert view.begin("apply_profile", PROFILE) == ("apply_profile", PROFILE)


@pytest.mark.parametrize("value", ["[]", "{}", "null", '{"api_version": 2}', "{bad"])
def test_incompatible_responses_raise_instead_of_showing_success(value):
    with pytest.raises(ValueError):
        presenter.decode_object(value)


def test_failed_command_reply_is_not_success_and_error_is_preserved():
    with pytest.raises(ValueError, match="Receiver asleep"):
        presenter.decode_result(json.dumps({"api_version": 1, "ok": False,
                                            "error": "Receiver asleep"}))
    assert presenter.decode_result('{"api_version":1,"ok":true,"result":{}}') == {}


def test_bad_state_cannot_leave_previous_connection_actions_enabled():
    view = ready()
    with pytest.raises(ValueError):
        view.update({"api_version": 1, "status": "invented"})
    assert not view.connected
    assert not view.availability("start").enabled


def test_state_changes_do_not_alias_input_or_erase_command_failure():
    state = session()
    view = ready()
    view.update(state)
    state["features"].append("calibrate")
    assert not view.availability("calibrate").enabled
    view.finish("start", "Receiver rejected connection")
    view.update(session())
    assert view.error == "Receiver rejected connection"


@pytest.mark.parametrize("updates", [{"status": []}, {"api_version": True},
                                      {"features": [None]}, {"trial": "bad"},
                                      {"trial": {"seconds_remaining": "bad"}}])
def test_malformed_state_is_rejected_without_crashing_render(updates):
    view = ready()
    with pytest.raises(ValueError):
        view.update(session(**updates))
    assert not view.connected


def test_restore_after_error_does_not_show_stale_health_as_live():
    view = ready(status="streaming", health={"temperature_c": 55, "cpu_percent": 20,
                                            "throttled_bits": 0})
    view.disconnect("Session closed")
    assert "unavailable" in view.health_text().lower()


def test_about_records_are_readable_without_raw_serialized_objects():
    text = presenter.about_text({"version": "0.1", "credits": [{"name": "Example contributor",
                                 "role": "Testing"}], "licenses": [{"name": "Example component",
                                 "license": "MIT", "source": "https://example.com/source"}]})
    assert "Example contributor" in text and "Testing" in text
    assert "MIT" in text and "https://example.com/source" in text
    assert "{" not in text and '"name"' not in text


@pytest.fixture
def gtk_window():
    """Run only when the integrator explicitly enables native desktop checks."""
    if os.environ.get("PANELBRIDGE_GTK_TESTS") != "1":
        pytest.skip("Native GTK check requires the integrator's desktop")
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gio, GLib, Gtk
    from panelbridge.ui.app import PanelBridgeWindow
    if not Gtk.init_check():
        pytest.skip("No GTK display")
    app = Gtk.Application(application_id="org.panelbridge.UiSmoke.t" + uuid.uuid4().hex,
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
    app.register(None)
    window = PanelBridgeWindow(app, preview=True, width=640, height=360)
    window.present()
    def settle():
        until = time.monotonic() + 0.12
        context = GLib.MainContext.default()
        while time.monotonic() < until:
            while context.pending():
                context.iteration(False)
            time.sleep(0.002)
    settle()
    yield window, settle
    window.close()
    settle()


def test_native_six_pages_fit_small_window_and_display_settings_scroll(gtk_window):
    window, settle = gtk_window
    assert window.client is None
    assert window.preview_banner.get_visible()
    for name in presenter.PAGES:
        window.show_page(name)
        settle()
        assert window.stack.get_visible_child_name() == name
        assert window.get_width() <= 640
        assert window.get_height() <= 360
    window.show_page("Display")
    settle()
    scroll = window.stack.get_visible_child()
    assert scroll.get_vadjustment().get_upper() > scroll.get_vadjustment().get_page_size()


def test_native_trial_bar_stays_visible_and_draft_survives_status_refresh(gtk_window):
    window, settle = gtk_window
    window.presenter.preview = False
    window._state_changed(session())
    window.fields["content_fps"].set_text("10")
    window._state_changed(session(health={"temperature_c": 50, "cpu_percent": 10,
                                         "throttled_bits": 0}))
    assert window.fields["content_fps"].get_text() == "10"
    window.show_page("Display")
    window._state_changed(session(trial={"kind": "profile", "seconds_remaining": 12}))
    settle()
    allocation = window.trial_box.get_allocation()
    assert window.trial_box.get_visible()
    assert allocation.height > 0
    assert allocation.y + allocation.height <= window.get_height()
    assert window.keep_button.get_sensitive()
    assert window.revert_button.get_sensitive()
    window._service_error("Controller disconnected")
    assert window.retry.get_visible()
    assert not window.keep_button.get_sensitive()
