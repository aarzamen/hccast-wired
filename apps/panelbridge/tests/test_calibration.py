"""Synthetic measurements exercise selection policy; no device claims or I/O."""

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.calibration import CalibrationPolicy, record_trial, select_presets
from panelbridge.models import Profile


MODES = [(1280, 720, 30), (1920, 1080, 30)]


def trial(name, *, fps=30, cpu=35, width=1280, duration=1200, power=None, observed=True):
    height = 1080 if width == 1920 else 720
    profile = Profile(source_width=width, source_height=height, content_fps=fps,
                      wire_width=width, wire_height=height)
    return {
        "trial_id": name, "context_id": "synthetic-context-v1", "workload_id": "synthetic-desktop-v1",
        "observed_at": "2026-10-02T00:00:00Z", "evidence_ref": f"trials/{name}.json",
        "profile": profile.to_dict(),
        "runtime_settings": {"encoder": "x264enc", "encoder_preset": "ultrafast",
                             "keyframe_interval_frames": 30, "cpu_governor": "ondemand",
                             "cpu_cap_mhz": 2400, "stock_clock": True,
                             "wifi_power_policy": "off"},
        "measurements": {
            "window": "steady_state", "warmup_s": 10,
            "elapsed_s": duration, "source_frames": int(duration * fps),
            "output_frames": int(duration * 30), "pipeline_dropped_frames": 0,
            "pipeline_drop_counts_complete": True, "decode_failures": 0,
            "disconnects": 0, "cpu_mean_percent": cpu, "temperature_peak_c": 60,
            "throttled_bits_before": 0, "throttled_bits_seen": 0,
            "health_samples": int(duration + 1), "health_max_gap_s": 1,
            "encoder_latency_ms": 12, "encoder_latency_statistic": "max",
            "encoder_latency_samples": int(duration * 30), "queue_growing": False,
            "negotiated_wire_mode": [width, height, 30], "whole_pi_power": power,
            "panel_latency_ms": None,
        },
        "observations": {"readable": observed, "geometry_correct": observed,
                         "motion_acceptable": observed},
    }


def choose(*trials, **kwargs):
    return select_presets(list(trials), context_id="synthetic-context-v1",
                          workload_id="synthetic-desktop-v1", advertised_modes=MODES, **kwargs)


def test_record_round_trip_keeps_exact_settings_without_aliasing_or_claim_upgrade():
    raw = trial("baseline", fps=15)
    recorded = record_trial(raw)
    raw["profile"]["content_fps"] = 30
    record = json.loads(json.dumps(recorded, allow_nan=False))
    assert record["profile"]["content_fps"] == 15
    assert record["profile"]["wire_fps"] == 30
    assert record["runtime_settings"]["cpu_cap_mhz"] == 2400
    assert record["measurements"]["panel_latency_ms"] is None


def test_measured_load_selects_recommended_performance_and_estimated_saver():
    low = trial("low", fps=15, cpu=15)
    balanced = trial("balanced", cpu=40)
    fast = trial("fast", width=1920, cpu=75)
    result = choose(low, balanced, fast)
    presets = result["presets"]
    assert presets["recommended"]["trial_ids"] == ["balanced"]
    assert presets["performance"]["trial_ids"] == ["fast"]
    assert presets["battery_saver"]["trial_ids"] == ["low"]
    assert presets["battery_saver"]["confidence"]["power"] == "estimated"
    assert "estimated" in presets["battery_saver"]["measurement"].lower()
    assert presets["battery_saver"]["confidence"]["panel_latency"] == "unmeasured"


def test_short_trials_produce_only_provisional_choices_until_thermal_and_visual_proof():
    result = choose(trial("short", duration=60, observed=None))
    assert result["shortlist"]["recommended"]["settings"]["content_fps"] == 30
    assert all(not item["available"] for item in result["presets"].values())
    reason = result["presets"]["recommended"]["reason"].lower()
    assert "thermal" in reason and "physical" in reason


@pytest.mark.parametrize("changes", [
    {"elapsed_s": 10}, {"temperature_peak_c": 70}, {"temperature_peak_c": 75},
    {"temperature_peak_c": None}, {"cpu_mean_percent": None},
    {"throttled_bits_seen": 1}, {"throttled_bits_seen": 4},
    {"throttled_bits_seen": 0x10000}, {"decode_failures": 1}, {"disconnects": 1},
    {"queue_growing": True}, {"queue_growing": None}, {"pipeline_dropped_frames": None},
    {"pipeline_drop_counts_complete": False}, {"encoder_latency_ms": 28},
    {"encoder_latency_statistic": "mean"}, {"encoder_latency_samples": 1},
    {"output_frames": 100}, {"source_frames": 100}, {"health_max_gap_s": 20},
    {"health_samples": 2}, {"negotiated_wire_mode": [1920, 1080, 30]},
])
def test_insufficient_or_unsafe_measurements_never_enable_a_preset(changes):
    raw = trial("rejected")
    raw["measurements"].update(changes)
    result = choose(raw)
    assert not result["assessments"][0]["software_passed"]
    assert result["assessments"][0]["reasons"]
    assert all(not item["available"] for item in result["presets"].values())


def test_one_percent_drop_boundary_is_rejected_without_inferred_receiver_drops():
    raw = trial("boundary", duration=30)
    raw["measurements"]["pipeline_dropped_frames"] = 9
    result = choose(raw)
    assert not result["assessments"][0]["software_passed"]
    assert result["assessments"][0]["pipeline_drop_fraction"] == pytest.approx(0.01)
    assert result["assessments"][0]["receiver_dropped_frames"] is None


def test_old_sticky_fault_is_distinct_from_new_or_active_fault():
    raw = trial("old-history")
    raw["measurements"].update(throttled_bits_before=0x50000, throttled_bits_seen=0x50000)
    assert choose(raw)["presets"]["recommended"]["available"]
    raw["measurements"]["throttled_bits_seen"] = 0x70000
    assert not choose(raw)["presets"]["recommended"]["available"]


def test_unadvertised_1080_and_false_physical_observation_are_not_assumed_safe():
    result = select_presets([trial("high", width=1920)], context_id="synthetic-context-v1",
                            workload_id="synthetic-desktop-v1", advertised_modes=[(1280, 720, 30)])
    assert not result["presets"]["performance"]["available"]
    result = choose(trial("unreadable", observed=False))
    assert not result["shortlist"]["recommended"]["available"]


def power(watts, uncertainty=0.1, *, meter="synthetic-meter"):
    return {"watts_mean": watts, "uncertainty_watts": uncertainty,
            "scope": "whole_pi_input", "meter_id": meter, "samples": 30}


def test_repeated_whole_pi_meter_results_override_cpu_only_saver_ranking():
    trials = [trial("cpu-low-a", fps=15, cpu=10, power=power(8)),
              trial("cpu-low-b", fps=15, cpu=12, power=power(8.1)),
              trial("power-low-a", cpu=30, power=power(6)),
              trial("power-low-b", cpu=31, power=power(6.1))]
    saver = choose(*trials)["presets"]["battery_saver"]
    assert saver["settings"]["content_fps"] == 30
    assert saver["confidence"]["power"] == "measured"
    assert "lowest measured" in saver["measurement"].lower()
    assert saver["power_comparison"]["candidate_count"] == 2


def test_partial_or_unrepeated_power_coverage_keeps_saver_estimated():
    saver = choose(trial("low", fps=15, cpu=10), trial("metered", cpu=30, power=power(6)))["presets"]["battery_saver"]
    assert saver["settings"]["content_fps"] == 15
    assert saver["confidence"]["power"] == "estimated"
    assert "lowest measured" not in saver["measurement"].lower()


def test_overlapping_power_uncertainty_never_claims_a_unique_lowest_candidate():
    records = [trial("a1", fps=15, cpu=10, power=power(6, 0.5)),
               trial("a2", fps=15, cpu=10, power=power(6.1, 0.5)),
               trial("b1", cpu=30, power=power(6.2, 0.5)),
               trial("b2", cpu=30, power=power(6.3, 0.5))]
    saver = choose(*records)["presets"]["battery_saver"]
    assert saver["confidence"]["power"] == "inconclusive"
    assert "lowest measured" not in saver["measurement"].lower()
    assert saver["power_comparison"]["uncertainty_overlap"]


def test_context_changes_invalidates_old_results_and_overclock_never_enters_presets():
    raw = trial("old")
    raw["context_id"] = "older-encoder"
    assert not choose(raw)["presets"]["recommended"]["available"]
    raw = trial("overclock")
    raw["runtime_settings"]["stock_clock"] = False
    assert not choose(raw)["presets"]["performance"]["available"]


@pytest.mark.parametrize("key,value", [("cpu_mean_percent", float("nan")),
                                      ("temperature_peak_c", float("inf")),
                                      ("output_frames", -1), ("elapsed_s", True)])
def test_malformed_numeric_record_is_rejected(key, value):
    raw = trial("malformed")
    raw["measurements"][key] = value
    with pytest.raises(ValueError):
        record_trial(raw)


def test_cpu_subwindow_survives_record_round_trip_without_extending_coverage():
    raw = trial("cpu-interval", duration=30)
    window = {"start_offset_s": .9, "end_offset_s": 28.9, "duration_s": 28,
              "coverage": 28 / 30, "intervals": 14}
    raw["measurements"]["cpu_window"] = window
    record = record_trial(json.loads(json.dumps(raw)))
    assert record["measurements"]["cpu_window"] == window
    assert record["measurements"]["elapsed_s"] == 30
    window["duration_s"] = 30
    assert record["measurements"]["cpu_window"]["duration_s"] == 28


def test_repeated_cpu_means_are_weighted_by_measured_cpu_time():
    full = trial("full-cpu", cpu=42)
    partial = trial("partial-cpu", cpu=80)
    partial["measurements"]["cpu_window"] = {
        "start_offset_s": 120, "end_offset_s": 1080, "duration_s": 960,
        "coverage": .8, "intervals": 480,
    }
    # Legacy full-window records and new explicit subwindows may coexist.
    result = choose(full, partial)
    preset = result["presets"]["recommended"]
    assert preset["available"]
    assert set(preset["trial_ids"]) == {"full-cpu", "partial-cpu"}


@pytest.mark.parametrize("changes", [
    {"start_offset_s": -1}, {"end_offset_s": 31}, {"start_offset_s": 29},
    {"duration_s": 30}, {"coverage": .7}, {"coverage": 1},
    {"coverage": float("nan")}, {"intervals": 0}, {"intervals": True},
    {"intervals": 1.5}, {"delivery_time": 9},
])
def test_malformed_cpu_subwindow_is_rejected(changes):
    raw = trial("bad-interval", duration=30)
    raw["measurements"]["cpu_window"] = {
        "start_offset_s": .9, "end_offset_s": 28.9, "duration_s": 28,
        "coverage": 28 / 30, "intervals": 14, **changes,
    }
    with pytest.raises(ValueError):
        record_trial(raw)


@pytest.mark.parametrize("changes", [{"elapsed_s": None}, {"elapsed_s": 0},
                                     {"cpu_mean_percent": None}])
def test_cpu_subwindow_requires_the_corresponding_measurements(changes):
    raw = trial("missing-interval", duration=30)
    raw["measurements"].update(changes)
    raw["measurements"]["cpu_window"] = {
        "start_offset_s": 1, "end_offset_s": 29, "duration_s": 28,
        "coverage": 28 / 30, "intervals": 14,
    }
    with pytest.raises(ValueError):
        record_trial(raw)


def test_duplicate_ids_and_unbounded_candidate_sets_are_rejected():
    raw = trial("same")
    with pytest.raises(ValueError, match="Duplicate"):
        choose(raw, copy.deepcopy(raw))
    records = [trial(f"trial-{index}") for index in range(33)]
    with pytest.raises(ValueError, match="bounded"):
        choose(*records)
    records = []
    for index in range(9):
        raw = trial(f"candidate-{index}")
        raw["profile"]["bitrate_kbps"] = 1000 + index * 100
        records.append(raw)
    with pytest.raises(ValueError, match="candidate"):
        choose(*records)


def test_missing_evidence_reference_or_non_pi_power_scope_is_not_recorded():
    raw = trial("missing")
    raw["evidence_ref"] = ""
    with pytest.raises(ValueError):
        record_trial(raw)
    raw = trial("wrong-meter", power={**power(8), "scope": "pi_and_receiver"})
    with pytest.raises(ValueError):
        record_trial(raw)


def test_policy_bounds_cannot_relax_heat_or_minimum_thermal_requirement():
    with pytest.raises(ValueError):
        CalibrationPolicy(max_temperature_c=80)
    with pytest.raises(ValueError):
        CalibrationPolicy(thermal_validation_s=30)


def test_transport_abort_is_retained_without_poisoning_later_rate_measurement():
    aborted = trial("association-timeout", fps=15)
    aborted.update(outcome="transport_aborted", failure_stage="association",
                   failure_reason="Receiver association timed out before capture started")
    aborted["measurements"] = {key: None for key in aborted["measurements"]}
    aborted["observations"] = {key: None for key in aborted["observations"]}
    result = choose(aborted, trial("measured-15", fps=15))
    failure = result["assessments"][0]
    assert failure["outcome"] == "transport_aborted"
    assert not failure["blocks_candidate"]
    assert not failure["software_passed"]
    assert result["presets"]["recommended"]["available"]
    assert result["presets"]["recommended"]["trial_ids"] == ["measured-15"]
    assert len(result["records"]) == 2


def test_stale_context_record_does_not_poison_matching_fresh_measurements():
    stale = trial("old-environment")
    stale["context_id"] = "previous-context"
    result = choose(stale, trial("fresh"))
    assert not result["assessments"][0]["software_passed"]
    assert result["presets"]["recommended"]["trial_ids"] == ["fresh"]


def test_startup_window_is_not_admitted_and_wire_duplication_is_not_a_drop():
    startup = trial("startup")
    startup["measurements"]["window"] = "startup"
    assert not choose(startup)["presets"]["recommended"]["available"]
    normal = trial("intentional-duplication", fps=15)
    assert normal["measurements"]["output_frames"] == 2 * normal["measurements"]["source_frames"]
    result = choose(normal)
    assert result["assessments"][0]["pipeline_drop_fraction"] == 0
    assert result["presets"]["recommended"]["available"]


def test_failed_measured_repeat_cannot_be_hidden_by_a_successful_repeat():
    bad = trial("unsafe-repeat")
    bad["measurements"]["temperature_peak_c"] = 75
    assert not choose(bad, trial("good-repeat"))["presets"]["recommended"]["available"]


def test_short_low_cpu_sample_cannot_hide_high_cpu_during_long_validation():
    result = choose(trial("short-idle", duration=30, cpu=0), trial("long-load", cpu=90))
    assert not result["presets"]["recommended"]["available"]
    assert result["presets"]["performance"]["available"]


def test_encoder_budget_uses_wire_rate_even_with_lower_content_rate():
    raw = trial("slow-content", fps=15)
    raw["measurements"]["encoder_latency_ms"] = 40
    assert not choose(raw)["presets"]["performance"]["available"]


def test_pipeline_latency_cannot_be_relabelled_as_measured_panel_latency():
    raw = trial("latency")
    raw["measurements"]["panel_latency_ms"] = 12
    with pytest.raises(ValueError, match="physical"):
        record_trial(raw)
