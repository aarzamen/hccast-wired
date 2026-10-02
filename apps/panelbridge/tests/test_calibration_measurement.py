"""Synthetic producer receipts only; these tests never run a stream or device."""

import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.calibration import CalibrationPolicy, record_trial, select_presets
from panelbridge.calibration_measurement import MeasurementAccumulator, record_transport_abort
from panelbridge.models import Profile


def metadata():
    return {
        "trial_id": "synthetic-trial", "context_id": "synthetic-context",
        "workload_id": "synthetic-workload", "observed_at": "2026-10-02T00:00:00Z",
        "evidence_ref": "trials/synthetic-trial.json", "profile": Profile().to_dict(),
        "runtime_settings": {"encoder": "x264enc", "encoder_preset": "ultrafast",
                             "keyframe_interval_frames": 30, "cpu_governor": "ondemand",
                             "cpu_cap_mhz": 2400, "stock_clock": None,
                             "wifi_power_policy": "off"},
    }


def health(second, *, cpu=40, temperature=55, bits=0, interval_start=None):
    return {"monotonic_us": int(second * 1_000_000), "cpu_percent": cpu,
            "temperature_c": temperature, "throttled_bits": bits,
            "cpu_interval": None if cpu is None or second <= 0 else {
                "start_us": int((second - 1 if interval_start is None else interval_start) * 1_000_000),
                "end_us": int(second * 1_000_000)}}


def native(second, **changes):
    frames = int(second * 30)
    result = {
        "event": "pipeline-telemetry", "metrics_version": 1,
        "monotonic_us": int(second * 1_000_000), "elapsed_us": int(second * 1_000_000),
        "final": False, "source_probe_available": True, "encoder_probes_available": True,
        "raw_frames_pushed": frames, "encoded_frames": frames,
        "encoded_buffers_with_unknown_frame_count": 0,
        "videorate_frames_dropped": 12, "videorate_frames_duplicated": frames,
        "appsrc_frames_dropped": None, "other_pipeline_frames_dropped": None,
        "pre_encoder_queue_buffers": 0, "pre_encoder_queue_media_duration_ns": 0,
        "pre_encoder_queue_residence_us": {"available": True, "oldest_pending_age_us": None},
        "encoder_sink_to_src_us": {
            "available": True, "matched": frames, "max_us": 12000,
            "invalid": 0, "unmatched": 0, "tracking_evicted": 0,
            "discontinuities": 1, "ambiguous": False,
        },
    }
    result.update(changes)
    return result


def accumulator(**changes):
    args = {"trial": metadata(), "stream_id": "synthetic-stream", "streaming_us": 0,
            "baseline_health": health(0, cpu=None), "negotiated_wire_mode": [1280, 720, 30]}
    args.update(changes)
    return MeasurementAccumulator(**args)


def feed(acc, start=1, end=35, *, bits=0):
    for second in range(start, end + 1):
        acc.add_health(health(second, bits=bits))
        acc.add_native("synthetic-stream", native(second))


def test_observed_window_deltas_exclude_warmup_and_preserve_unknowns():
    raw = metadata()
    acc = accumulator(trial=raw)
    raw["runtime_settings"]["stock_clock"] = True
    feed(acc)
    result = acc.finish(35_000_000)
    record = result["record"]
    metrics = record["measurements"]
    assert record_trial(json.loads(json.dumps(record))) == record
    assert (metrics["warmup_s"], metrics["elapsed_s"]) == (5, 30)
    assert (metrics["source_frames"], metrics["output_frames"]) == (900, 900)
    assert metrics["encoder_latency_ms"] == 12  # Lifetime max, never max-minus-max.
    assert metrics["encoder_latency_samples"] == 900
    assert metrics["cpu_mean_percent"] == 40
    assert metrics["health_samples"] == 31
    assert metrics["health_max_gap_s"] == 1
    assert result["duration_satisfied"] is True
    assert result["thermal_duration_satisfied"] is False
    assert result["known_drop_deltas"]["videorate_frames_dropped"] == 0
    for key in ("pipeline_dropped_frames", "decode_failures", "disconnects", "queue_growing",
                "panel_latency_ms", "whole_pi_power"):
        assert metrics[key] is None
    assert metrics["pipeline_drop_counts_complete"] is False
    assert record["runtime_settings"]["stock_clock"] is None
    assert set(record["observations"].values()) == {None}
    selected = select_presets([record], context_id="synthetic-context",
                              workload_id="synthetic-workload", advertised_modes=[(1280, 720, 30)])
    assert not any(item["available"] for item in selected["presets"].values())


def test_repeated_gui_samples_do_not_add_time_or_frames():
    acc = accumulator()
    feed(acc, end=5)
    for _ in range(500):
        assert acc.add_native("synthetic-stream", native(5)) is False
        assert acc.add_health(health(5)) is False
    result = acc.finish(5_000_000)
    assert result["record"]["measurements"]["elapsed_s"] == 0
    assert result["retained_samples"] == 11
    assert "measurement_too_short" in result["failure_reasons"]


@pytest.mark.parametrize("kind", ["conflicting_duplicate", "backwards", "epoch", "counter_reset"])
def test_bad_producer_history_stops_and_cannot_become_steady_state(kind):
    acc = accumulator()
    feed(acc, end=6)
    if kind == "conflicting_duplicate":
        acc.add_native("synthetic-stream", native(6, raw_frames_pushed=999))
    elif kind == "backwards":
        acc.add_native("synthetic-stream", native(5.5))
    elif kind == "epoch":
        acc.add_native("another-stream", native(7))
    else:
        acc.add_native("synthetic-stream", native(7, raw_frames_pushed=1))
    assert acc.should_stop
    result = acc.finish(7_000_000)
    assert result["stop_reasons"]
    assert result["record"]["measurements"]["window"] is None


@pytest.mark.parametrize("second,bits", [(2, 0x10000), (10, 0x30000)])
def test_new_sticky_fault_stops_even_during_warmup_with_fixed_baseline(second, bits):
    acc = accumulator(baseline_health=health(0, cpu=None, bits=0x10000 if second == 10 else 0))
    feed(acc, end=second - 1, bits=0x10000 if second == 10 else 0)
    acc.add_health(health(second, bits=bits))
    assert acc.should_stop
    result = acc.finish(second * 1_000_000)
    assert "new_sticky_fault" in result["stop_reasons"]
    assert result["first_stop_us"] == second * 1_000_000
    assert result["record"]["measurements"]["throttled_bits_seen"] == bits


def test_old_history_is_retained_without_vetoing_the_window():
    acc = accumulator(baseline_health=health(0, cpu=None, bits=0x10000))
    feed(acc, bits=0x10000)
    result = acc.finish(35_000_000)
    assert not result["stop_reasons"]
    assert result["record"]["measurements"]["throttled_bits_before"] == 0x10000


@pytest.mark.parametrize("sample", [health(1, temperature=70), health(1, temperature=75),
                                    health(1, bits=2), health(1, bits=8), health(1, bits=0x10),
                                    health(1, temperature=None), health(1, bits=None)])
def test_critical_health_fails_closed(sample):
    acc = accumulator()
    acc.add_health(sample)
    assert acc.should_stop


def test_health_gap_checked_without_waiting_for_another_health_sample():
    acc = accumulator()
    feed(acc, end=5)
    acc.add_native("synthetic-stream", native(10))
    acc.check_deadline(10_000_001)
    assert acc.should_stop
    result = acc.finish(10_000_001)
    assert "health_gap" in result["stop_reasons"]
    assert result["record"]["measurements"]["health_max_gap_s"] == 5


def test_cpu_weighting_and_missing_interval_are_honest():
    acc = accumulator()
    feed(acc, end=5)
    acc.add_health(health(6, cpu=10))
    acc.add_health(health(10, cpu=50, interval_start=6))
    acc.add_native("synthetic-stream", native(10))
    assert acc.finish(10_000_000)["record"]["measurements"]["cpu_mean_percent"] == 42
    acc = accumulator()
    feed(acc, end=5)
    acc.add_health(health(6, cpu=None))
    acc.add_native("synthetic-stream", native(6))
    assert acc.finish(6_000_000)["record"]["measurements"]["cpu_mean_percent"] is None


def test_unaligned_cpu_boundaries_do_not_average_warmup_into_window():
    acc = accumulator()
    for second in range(1, 8):
        acc.add_health(health(second - 0.1))
        acc.add_native("synthetic-stream", native(second))
    assert acc.finish(7_000_000)["record"]["measurements"]["cpu_mean_percent"] is None


def test_cpu_uses_measured_contiguous_subwindow_despite_independent_producer_phase():
    acc = accumulator()
    for second in range(1, 36):
        if second % 2 == 0:
            # Actual CPU counter reads precede slow sensor and IPC completion.
            sample = health(second - .1, interval_start=max(0, second - 2.1))
            sample["monotonic_us"] = second * 1_000_000
            acc.add_health(sample)
        acc.add_native("synthetic-stream", native(second))
    metrics = acc.finish(35_000_000)["record"]["measurements"]
    assert metrics["cpu_mean_percent"] == 40
    assert metrics["cpu_window"] == {
        "start_offset_s": .9, "end_offset_s": 28.9, "duration_s": 28,
        "coverage": 28 / 30, "intervals": 14,
    }


def test_cpu_without_producer_interval_is_not_retimed_from_delivery():
    acc = accumulator()
    for second in range(1, 36):
        sample = health(second)
        sample.pop("cpu_interval")
        acc.add_health(sample)
        acc.add_native("synthetic-stream", native(second))
    metrics = acc.finish(35_000_000)["record"]["measurements"]
    assert metrics["cpu_mean_percent"] is None
    assert metrics["cpu_window"] is None


@pytest.mark.parametrize("interval", [
    {"start_us": 7_000_000, "end_us": 6_000_000},
    {"start_us": 5_000_000, "end_us": 7_000_000},
    {"start_us": 5_000_000, "end_us": 6_000_000, "untrusted": True},
])
def test_invalid_cpu_interval_cannot_supply_an_aggregate(interval):
    acc = accumulator()
    feed(acc, end=5)
    bad = health(6)
    bad["cpu_interval"] = interval
    acc.add_health(bad)
    acc.add_native("synthetic-stream", native(6))
    assert acc.finish(6_000_000)["record"]["measurements"]["cpu_mean_percent"] is None


@pytest.mark.parametrize("change", ["unknown_framing", "missing_probe", "timing_eviction", "segment"])
def test_missing_probe_or_timing_quality_cannot_become_measured_zero(change):
    acc = accumulator()
    feed(acc, end=5)
    sample = native(6)
    if change == "unknown_framing":
        sample["encoded_frames"] = None
        sample["encoded_buffers_with_unknown_frame_count"] = 1
    elif change == "missing_probe":
        sample["encoder_probes_available"] = False
    elif change == "timing_eviction":
        sample["encoder_sink_to_src_us"]["tracking_evicted"] = 1
    else:
        sample["encoder_sink_to_src_us"]["discontinuities"] = 2
    acc.add_health(health(6))
    acc.add_native("synthetic-stream", sample)
    metrics = acc.finish(6_000_000)["record"]["measurements"]
    if change in ("unknown_framing", "missing_probe"):
        assert metrics["output_frames"] is None
    else:
        assert metrics["encoder_latency_ms"] is None


def test_native_end_snapshot_not_requested_finish_controls_duration_and_teardown_excluded():
    acc = accumulator()
    feed(acc)
    acc.add_native("synthetic-stream", native(36, final=True, raw_frames_pushed=99999))
    result = acc.finish(36_000_000)
    assert result["record"]["measurements"]["elapsed_s"] == 30
    assert result["record"]["measurements"]["source_frames"] == 900
    assert result["window_end_us"] == 35_000_000


def test_thermal_gate_excludes_warmup_and_retention_is_bounded():
    acc = accumulator(thermal=True)
    feed(acc, end=1204)
    result = acc.finish(1204_000_000)
    assert result["duration_satisfied"] is False
    assert result["thermal_duration_satisfied"] is False
    acc = accumulator(thermal=True)
    feed(acc, end=1205)
    assert acc.finish(1205_000_000)["thermal_duration_satisfied"] is True
    acc = accumulator(max_samples=16)
    feed(acc, end=100)
    result = acc.finish(100_000_000)
    assert result["retained_samples"] <= 16
    assert "sample_limit" in result["stop_reasons"]


def test_explicit_physical_power_values_validate_and_are_copied():
    acc = accumulator()
    feed(acc)
    observations = {"readable": True, "geometry_correct": False, "motion_acceptable": None}
    result = acc.finish(35_000_000, observations=observations,
                        panel_latency={"panel_latency_ms": 120,
                                       "panel_latency_method": "physical_marker_capture",
                                       "panel_latency_evidence_ref": "trials/markers.json"})
    observations["geometry_correct"] = True
    assert result["record"]["observations"]["geometry_correct"] is False
    assert result["record"]["measurements"]["panel_latency_ms"] == 120
    with pytest.raises(RuntimeError, match="finished"):
        acc.add_health(health(36))


def test_transport_abort_retains_unknowns_and_cannot_claim_capture_frames():
    result = record_transport_abort(metadata(), stage="association", reason="Negotiation timed out")
    assert result["outcome"] == "transport_aborted"
    assert result["measurements"]["source_frames"] is None
    with pytest.raises(ValueError):
        record_transport_abort(metadata(), stage="encoding", reason="bad")


def test_bad_metadata_and_timestamps_rejected_without_mutating_caller():
    raw = metadata()
    saved = copy.deepcopy(raw)
    with pytest.raises(ValueError):
        accumulator(streaming_us=float("nan"), trial=raw)
    assert raw == saved
    with pytest.raises(ValueError):
        accumulator(policy=CalibrationPolicy(min_trial_s=29))


def test_known_emergency_with_missing_other_sensor_is_not_complete_health_coverage():
    acc = accumulator()
    feed(acc, end=5)
    acc.add_health(health(6, temperature=None, bits=1))
    acc.add_native("synthetic-stream", native(6))
    metrics = acc.finish(6_000_000)["record"]["measurements"]
    assert metrics["health_samples"] == 1
    assert metrics["temperature_peak_c"] is None


def test_tightened_temperature_policy_stops_at_its_own_limit():
    acc = accumulator(policy=CalibrationPolicy(max_temperature_c=65))
    acc.add_health(health(1, temperature=65))
    assert acc.should_stop


def test_deadline_clock_cannot_run_backwards():
    acc = accumulator()
    acc.check_deadline(1_000_000)
    with pytest.raises(ValueError, match="backwards"):
        acc.check_deadline(500_000)


def test_boundary_gap_includes_no_valid_health_after_measured_start():
    acc = accumulator()
    feed(acc, end=5)
    acc.add_native("synthetic-stream", native(10))
    acc.add_native("synthetic-stream", native(15))
    result = acc.finish(15_000_000)
    assert result["record"]["measurements"]["health_max_gap_s"] == 10
    assert "health_gap" in result["stop_reasons"]


def test_missing_cpu_interval_is_not_hidden_by_later_good_samples():
    acc = accumulator()
    feed(acc, end=5)
    acc.add_health(health(6, cpu=None))
    acc.add_native("synthetic-stream", native(6))
    feed(acc, start=7)
    result = acc.finish(35_000_000)
    assert result["record"]["measurements"]["cpu_mean_percent"] is None
    assert "cpu_coverage_unknown" in result["failure_reasons"]


def test_native_delta_never_counts_content_to_wire_duplication_as_loss():
    raw = metadata()
    raw["profile"]["content_fps"] = 15
    acc = accumulator(trial=raw)
    for second in range(1, 36):
        acc.add_health(health(second))
        acc.add_native("synthetic-stream", native(second, raw_frames_pushed=second * 15))
    result = acc.finish(35_000_000)
    metrics = result["record"]["measurements"]
    assert metrics["source_frames"] == 450
    assert metrics["output_frames"] == 900
    assert metrics["pipeline_dropped_frames"] is None


def cohort(second, *, closed=False):
    """Synthetic v1 shape independently modeled on the actual native receipt.

    Source15/wire30, source window5.25..35.25; native STREAMING at0.25.
    The extra0.25 seconds to EOS must never enter measured duration or CPU.
    """
    started = second >= 5.25
    accepted = 450 if closed else max(0, int((second - 5.25) * 15))
    outputs = accepted * 2
    return {
        "version": 1, "enabled": True, "scope": "accepted-source-to-encoder-sink",
        "complete": closed, "closed": closed, "requested_warmup_us": 5_000_000,
        "requested_duration_us": 30_000_000, "streaming_us": 250_000,
        "invalid_reason": None if closed else "source-window-not-ended" if started else "cohort-not-started",
        "epoch": 5_250_000 if started else None, "started_us": 5_250_000 if started else None,
        "source_seq_first": 101 if started else None,
        "ended_us": 35_250_000 if closed else None,
        "source_seq_end_exclusive": 551 if closed else None,
        "closed_us": 35_500_000 if closed else None, "encoder_src_eos_seen": closed,
        "accepted_source_frames": accepted, "rejected_source_frames": 0,
        "emitted_wire_frames": outputs, "encoded_frames": outputs,
        "encoder_output_key_unknown": False,
        "encoder_sink_to_src_us": {
            "available": True, "entered": outputs, "exited": outputs, "matched": outputs,
            "unmatched": 0, "tracking_evicted": 0, "invalid": 0, "discontinuities": 0,
            "ambiguous": False, "last_us": 6000 if outputs else None,
            "max_us": 12000 if outputs else None, "mean_us": 8000 if outputs else None,
            "oldest_pending_age_us": None,
        },
        "rate_source_frames_dropped": 0, "rate_frames_duplicated": accepted,
        "boundary_frames_dropped": dict.fromkeys(("appsrc", "source_to_rate", "rate_to_scale", "scale",
                                                  "scale_to_convert", "convert", "convert_to_queue",
                                                  "queue", "queue_to_encoder"), 0),
        "pipeline_frames_dropped": 0 if closed else None,
    }


def feed_cohort(*, closed_change=None, final=True, progress_change=None):
    raw = metadata()
    raw["profile"]["content_fps"] = 15
    acc = accumulator(trial=raw)
    for second in range(1, 36):
        acc.add_health(health(second))
        sample = native(second, raw_frames_pushed=second * 15,
                        local_frame_accounting=cohort(second))
        if progress_change and second == 20:
            progress_change(sample["local_frame_accounting"])
        acc.add_native("synthetic-stream", sample)
    terminal = native(35.6, final=final, raw_frames_pushed=534,
                      local_frame_accounting=cohort(35.6, closed=True))
    if closed_change:
        closed_change(terminal["local_frame_accounting"])
    acc.add_native("synthetic-stream", terminal)
    return acc, terminal


@pytest.mark.parametrize("final", [False, True])
def test_closed_cohort_uses_source_window_and_preserves_receiver_unknowns(final):
    acc, terminal = feed_cohort(final=final)
    terminal["local_frame_accounting"]["accepted_source_frames"] = 99999
    acc.add_health(health(36, cpu=99, temperature=69))  # Drain/receipt period, outside source window.
    result = acc.finish(36_000_000)
    metrics = result["record"]["measurements"]
    assert record_trial(json.loads(json.dumps(result["record"]))) == result["record"]
    assert (result["window_start_us"], result["window_end_us"], result["cohort_drain_end_us"]) == (
        5_250_000, 35_250_000, 35_500_000)
    assert (metrics["warmup_s"], metrics["elapsed_s"]) == (5, 30)
    assert (metrics["source_frames"], metrics["output_frames"]) == (450, 900)
    assert metrics["pipeline_dropped_frames"] == 0
    assert metrics["pipeline_drop_counts_complete"] is True
    assert (metrics["encoder_latency_ms"], metrics["encoder_latency_samples"]) == (12, 900)
    assert metrics["temperature_peak_c"] == 55
    assert metrics["cpu_mean_percent"] == 40
    assert metrics["cpu_window"]["duration_s"] == 29
    assert metrics["cpu_window"]["start_offset_s"] == .75
    assert metrics["cpu_window"]["coverage"] == 29 / 30
    assert all(row["monotonic_us"] <= 35_250_000 for row in result["queue_observations"])
    assert result["local_frame_accounting"]["accepted_source_frames"] == 450
    assert "pipeline_loss_unknown" not in result["failure_reasons"]
    for name in ("decode_failures", "disconnects", "queue_growing", "panel_latency_ms"):
        assert metrics[name] is None


def test_cohort_drop_and_duplicate_that_balance_are_not_erased():
    def change(record):
        record["rate_frames_duplicated"] = 451
        record["boundary_frames_dropped"]["convert"] = 1
        record["pipeline_frames_dropped"] = 1
    acc, _ = feed_cohort(closed_change=change)
    metrics = acc.finish(35_600_000)["record"]["measurements"]
    assert (metrics["source_frames"], metrics["output_frames"], metrics["pipeline_dropped_frames"]) == (450, 900, 1)
    assert metrics["pipeline_drop_counts_complete"] is True


@pytest.mark.parametrize("change", [
    lambda c: c.update(version=True),
    lambda c: c.update(scope="receiver-delivery"),
    lambda c: c.update(complete=1),
    lambda c: c.update(closed=False),
    lambda c: c.update(invalid_reason="missing-frame-metadata"),
    lambda c: c.update(epoch=5_250_001),
    lambda c: c.update(source_seq_end_exclusive=552),
    lambda c: c.update(accepted_source_frames=True),
    lambda c: c.update(encoded_frames=901),
    lambda c: c.update(pipeline_frames_dropped=1),
    lambda c: c["boundary_frames_dropped"].pop("appsrc"),
    lambda c: c["boundary_frames_dropped"].update(transport=0),
    lambda c: c["boundary_frames_dropped"].update(appsrc=-1),
    lambda c: c.update(started_us=36_000_000),
    lambda c: c.update(ended_us=35_000_000),
    lambda c: c.update(closed_us=35_700_000),
    lambda c: c.update(requested_warmup_us=4_000_000),
    lambda c: c.update(requested_duration_us=29_000_000),
    lambda c: c["encoder_sink_to_src_us"].update(matched=899),
    lambda c: c["encoder_sink_to_src_us"].update(entered=901),
    lambda c: c["encoder_sink_to_src_us"].update(max_us=float("nan")),
])
def test_manipulated_complete_cohort_cannot_become_measured_loss(change):
    acc, _ = feed_cohort(closed_change=change)
    result = acc.finish(35_600_000)
    assert result["record"]["measurements"]["pipeline_dropped_frames"] is None
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False
    assert result["record"]["measurements"]["window"] is None
    assert result["stop_reasons"]


def test_preencoder_eos_without_encoder_output_eos_is_not_a_completed_trial():
    acc, _ = feed_cohort(closed_change=lambda c: c.update(encoder_src_eos_seen=False))
    result = acc.finish(35_600_000)
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False
    assert "cohort_incomplete" in result["failure_reasons"]


def test_earlier_cohort_identity_conflict_cannot_be_hidden_by_valid_terminal_record():
    acc, _ = feed_cohort(progress_change=lambda c: c.update(epoch=9_000_000, started_us=9_000_000))
    result = acc.finish(35_600_000)
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False
    assert result["record"]["measurements"]["window"] is None


def test_post_eos_count_change_invalidates_previously_closed_cohort():
    acc, _ = feed_cohort(final=False)
    changed = cohort(36, closed=True)
    changed.update(accepted_source_frames=451, source_seq_end_exclusive=552, rate_frames_duplicated=449)
    acc.add_native("synthetic-stream", native(36, final=True, raw_frames_pushed=540,
                                              local_frame_accounting=changed))
    result = acc.finish(36_000_000)
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


@pytest.mark.parametrize("quality", ["unknown_key", "eviction", "ambiguous"])
def test_closed_loss_scope_does_not_invent_unknown_encoded_count_or_latency(quality):
    def change(record):
        record["encoded_frames"] = None
        if quality == "unknown_key":
            record["encoder_output_key_unknown"] = True
        elif quality == "eviction":
            record["encoder_sink_to_src_us"]["tracking_evicted"] = 1
        else:
            record["encoder_sink_to_src_us"]["ambiguous"] = True
    acc, _ = feed_cohort(closed_change=change)
    metrics = acc.finish(35_600_000)["record"]["measurements"]
    assert metrics["pipeline_drop_counts_complete"] is True
    assert metrics["pipeline_dropped_frames"] == 0
    assert metrics["output_frames"] is None
    assert metrics["encoder_latency_ms"] is None


def test_conflicting_terminal_duplicate_cannot_reuse_previously_valid_cohort():
    acc, terminal = feed_cohort(final=False)
    terminal["local_frame_accounting"]["boundary_frames_dropped"]["queue"] = 1
    acc.add_native("synthetic-stream", terminal)
    result = acc.finish(35_600_000)
    assert "native_conflicting_duplicate" in result["stop_reasons"]
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


def test_closed_cohort_cannot_cross_earlier_cumulative_counter_reset():
    raw = metadata()
    raw["profile"]["content_fps"] = 15
    acc = accumulator(trial=raw)
    for second in range(1, 36):
        acc.add_health(health(second))
        acc.add_native("synthetic-stream", native(second, raw_frames_pushed=0 if second == 20 else second * 15,
                                                  local_frame_accounting=cohort(second)))
    acc.add_native("synthetic-stream", native(35.6, local_frame_accounting=cohort(35.6, closed=True)))
    result = acc.finish(35_600_000)
    assert "native_counter_reset" in result["stop_reasons"]
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


def test_missing_cohort_mid_run_cannot_be_hidden_by_valid_close():
    acc, _ = feed_cohort(progress_change=lambda c: c.update(enabled=False, complete=False, closed=False))
    result = acc.finish(35_600_000)
    assert "cohort_missing" in result["stop_reasons"]
    assert result["record"]["measurements"]["pipeline_dropped_frames"] is None


def test_encoded_output_can_be_counted_while_unresolved_input_latency_stays_unknown():
    def change(record):
        record["encoded_frames"] = 899
        record["encoder_sink_to_src_us"].update(exited=899, matched=899, oldest_pending_age_us=500_000)
    acc, _ = feed_cohort(closed_change=change)
    metrics = acc.finish(35_600_000)["record"]["measurements"]
    assert metrics["pipeline_drop_counts_complete"] is True
    assert metrics["output_frames"] == 899
    assert metrics["encoder_latency_ms"] is None


def test_no_cohort_close_retains_partial_counts_without_loss_or_duration_certification():
    acc = accumulator()
    for second in range(1, 36):
        acc.add_health(health(second))
        acc.add_native("synthetic-stream", native(second, local_frame_accounting=cohort(second)))
    result = acc.finish(35_000_000)
    assert result["record"]["measurements"]["source_frames"] == 900
    assert result["record"]["measurements"]["pipeline_dropped_frames"] is None
    assert result["record"]["measurements"]["window"] is None
    assert result["duration_satisfied"] is False


def test_cohort_first_seen_only_at_close_cannot_supply_unobserved_identity_history():
    acc = accumulator()
    feed(acc)
    acc.add_native("synthetic-stream", native(35.6, final=True, local_frame_accounting=cohort(35.6, closed=True)))
    result = acc.finish(35_600_000)
    assert "cohort_window_invalid" in result["stop_reasons"]
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


def test_closed_cohort_cannot_backdate_a_start_reported_absent_during_streaming():
    acc = accumulator()
    for second in range(1, 36):
        acc.add_health(health(second))
        # The producer reports no cohort start even after the claimed start.
        acc.add_native("synthetic-stream", native(second, local_frame_accounting=cohort(1)))
    acc.add_native("synthetic-stream", native(35.6, final=True,
                                              local_frame_accounting=cohort(35.6, closed=True)))
    result = acc.finish(35_600_000)
    assert "cohort_boundary_backdated" in result["stop_reasons"]
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


@pytest.mark.parametrize("probe", ["source_probe_available", "encoder_probes_available"])
def test_final_cohort_cannot_override_explicitly_unavailable_native_probe(probe):
    acc, terminal = feed_cohort(final=False)
    terminal.update(final=True, monotonic_us=36_000_000, **{probe: False})
    acc.add_native("synthetic-stream", terminal)
    result = acc.finish(36_000_000)
    assert result["record"]["measurements"]["pipeline_drop_counts_complete"] is False


def test_drain_cannot_turn_a_short_thermal_window_into_twenty_minutes():
    # Keep real time boundaries explicit; this is synthetic receipt validation,
    # not a claim that a 20-minute hardware trial ran.
    acc = accumulator(thermal=True)
    for second in range(1, 1205):
        acc.add_health(health(second))
        c = cohort(second)
        c["requested_duration_us"] = 1_199_500_000
        acc.add_native("synthetic-stream", native(second, local_frame_accounting=c))
    c = cohort(1205.6, closed=True)
    c.update(requested_duration_us=1_199_500_000, ended_us=1_204_750_000,
             closed_us=1_205_500_000, accepted_source_frames=17993,
             emitted_wire_frames=35986, encoded_frames=35986, rate_frames_duplicated=17993,
             source_seq_end_exclusive=18094)
    c["encoder_sink_to_src_us"].update(entered=35986, exited=35986, matched=35986)
    acc.add_health(health(1205))
    acc.add_native("synthetic-stream", native(1205.6, final=True, local_frame_accounting=c))
    result = acc.finish(1_205_600_000)
    assert result["record"]["measurements"]["elapsed_s"] == 1199.5
    assert result["thermal_duration_satisfied"] is False
    assert result["duration_satisfied"] is False


def test_unrecognized_native_reason_is_not_copied_into_exportable_diagnostics():
    acc, _ = feed_cohort(closed_change=lambda c: c.update(complete=False, pipeline_frames_dropped=None,
                                                        invalid_reason="untrusted producer text"))
    result = acc.finish(35_600_000)
    assert result["local_frame_accounting"]["invalid_reason"] == "native-reported-invalid"
    assert "untrusted producer text" not in json.dumps(result)
