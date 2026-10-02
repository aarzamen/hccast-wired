"""Pure calibration records and conservative selection; never runs hardware."""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from pathlib import PurePosixPath
import statistics

from .models import Profile


PRESET_NAMES = ("recommended", "performance", "battery_saver")
RUNTIME_FIELDS = {"encoder", "encoder_preset", "keyframe_interval_frames", "cpu_governor",
                  "cpu_cap_mhz", "stock_clock", "wifi_power_policy"}
NUMERIC_FIELDS = {
    "warmup_s": (0, 14400, False),
    "elapsed_s": (0, 14400, False), "source_frames": (0, 10**8, True),
    "output_frames": (0, 10**8, True), "pipeline_dropped_frames": (0, 10**8, True),
    "decode_failures": (0, 10**8, True), "disconnects": (0, 10**6, True),
    "cpu_mean_percent": (0, 100, False), "temperature_peak_c": (-20, 150, False),
    "throttled_bits_before": (0, 0xFFFFFFFF, True),
    "throttled_bits_seen": (0, 0xFFFFFFFF, True), "health_samples": (0, 10**7, True),
    "health_max_gap_s": (0, 14400, False), "encoder_latency_ms": (0, 14400000, False),
    "encoder_latency_samples": (0, 10**8, True), "panel_latency_ms": (0, 14400000, False),
}


@dataclass(frozen=True)
class CalibrationPolicy:
    min_trial_s: float = 30
    min_warmup_s: float = 5
    thermal_validation_s: float = 1200
    max_temperature_c: float = 70
    min_encoder_headroom: float = 0.20
    max_pipeline_drop_fraction: float = 0.01
    min_frame_ratio: float = 0.98
    max_frame_ratio: float = 1.05
    max_health_gap_s: float = 5
    recommended_cpu_percent: float = 60
    max_candidates: int = 8
    max_trials: int = 32

    def __post_init__(self):
        bounds = {
            "min_warmup_s": (5, 60),
            "min_trial_s": (30, 14400), "thermal_validation_s": (1200, 14400),
            "max_temperature_c": (1, 70), "min_encoder_headroom": (0.20, 0.99),
            "max_pipeline_drop_fraction": (0.000001, 0.01), "min_frame_ratio": (0.98, 1),
            "max_frame_ratio": (1, 1.05), "max_health_gap_s": (0.1, 5),
            "recommended_cpu_percent": (1, 100), "max_candidates": (1, 8),
            "max_trials": (1, 32),
        }
        for key, (low, high) in bounds.items():
            _numeric(getattr(self, key), key, low, high, integer=key.startswith("max_cand") or key == "max_trials")


def _numeric(value, name, low, high, *, integer=False, optional=False):
    if value is None and optional:
        return
    if (type(value) not in (int, float) or not low <= value <= high
            or not math.isfinite(value) or (integer and type(value) is not int)):
        raise ValueError(f"{name} must be a finite {'integer' if integer else 'number'} in {low}..{high}")


def _text(value, name, *, limit=160):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(char) < 32 for char in value)):
        raise ValueError(f"{name} needs a bounded, nonempty text value")


def _reference(value, name):
    _text(value, name, limit=240)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or ":" in value or "\\" in value:
        raise ValueError(f"{name} must be a relative evidence reference")


def _mode(value):
    if (not isinstance(value, (list, tuple)) or len(value) != 3
            or any(type(item) is not int for item in value)
            or tuple(value) not in ((1280, 720, 30), (1920, 1080, 30))):
        raise ValueError("Wire mode must be a supported width, height, refresh tuple")
    return tuple(value)


def record_trial(value):
    """Validate and copy one externally measured trial into JSON-safe API v1 data.

    Unknown numeric measurements are retained as None. They never become zero.
    This function checks structure, not truth: the runner supplies real evidence.
    """
    if not isinstance(value, dict):
        raise ValueError("A trial record must be an object")
    required = {"trial_id", "context_id", "workload_id", "observed_at", "evidence_ref",
                "profile", "runtime_settings", "measurements", "observations"}
    if not required <= value.keys() or set(value) - required - {
        "api_version", "outcome", "failure_stage", "failure_reason"
    }:
        raise ValueError("A complete trial record with only documented fields is required")
    if "api_version" in value and (type(value["api_version"]) is not int or value["api_version"] != 1):
        raise ValueError("Unsupported calibration record version")
    result = copy.deepcopy(value)
    result["api_version"] = 1
    result.setdefault("outcome", "completed")
    if result["outcome"] not in ("completed", "transport_aborted"):
        raise ValueError("Trial outcome must be completed or transport_aborted")
    if result["outcome"] == "transport_aborted":
        if result.get("failure_stage") not in ("discovery", "association", "rtsp_setup"):
            raise ValueError("Aborted transport attempts need a pre-capture failure stage")
        _text(result.get("failure_reason"), "failure_reason", limit=400)
    elif "failure_stage" in result or "failure_reason" in result:
        raise ValueError("Failure-stage fields belong to aborted transport attempts")
    for key in ("trial_id", "context_id", "workload_id"):
        _text(result[key], key)
    _reference(result["evidence_ref"], "evidence_ref")
    try:
        when = datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
        if when.tzinfo is None:
            raise ValueError("Timestamp needs a timezone")
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError("observed_at needs an ISO timestamp with timezone") from error
    profile = result["profile"]
    result["profile"] = (profile if isinstance(profile, Profile) else Profile.from_dict(profile)).to_dict()
    runtime = result["runtime_settings"]
    if not isinstance(runtime, dict) or set(runtime) != RUNTIME_FIELDS:
        raise ValueError("Exact runtime settings are required")
    for key in ("encoder", "encoder_preset", "cpu_governor", "wifi_power_policy"):
        if runtime[key] is not None:
            _text(runtime[key], key, limit=80)
    for key, high in (("keyframe_interval_frames", 10000), ("cpu_cap_mhz", 2700)):
        _numeric(runtime[key], key, 1, high, integer=True, optional=True)
    if runtime["stock_clock"] is not None and type(runtime["stock_clock"]) is not bool:
        raise ValueError("stock_clock must be observed true, false or unknown")
    observations = result["observations"]
    if not isinstance(observations, dict) or set(observations) != {
        "readable", "geometry_correct", "motion_acceptable"
    }:
        raise ValueError("Readability, geometry and motion observations are required")
    if any(item is not None and type(item) is not bool for item in observations.values()):
        raise ValueError("Physical observations must be true, false or unknown")
    metrics = result["measurements"]
    if not isinstance(metrics, dict):
        raise ValueError("Measured trial aggregates are required")
    other = {"window", "pipeline_drop_counts_complete", "encoder_latency_statistic", "queue_growing", "cpu_window",
             "negotiated_wire_mode", "whole_pi_power", "panel_latency_method", "panel_latency_evidence_ref"}
    required_metrics = set(NUMERIC_FIELDS) | (other - {"panel_latency_method", "panel_latency_evidence_ref", "cpu_window"})
    if not required_metrics <= metrics.keys() or set(metrics) - required_metrics - other:
        raise ValueError("Complete documented measurement fields are required; use null for unknowns")
    for key, (low, high, integer) in NUMERIC_FIELDS.items():
        _numeric(metrics[key], key, low, high, integer=integer, optional=True)
    cpu_window = metrics.get("cpu_window")
    if cpu_window is not None:
        if not isinstance(cpu_window, dict) or set(cpu_window) != {
            "start_offset_s", "end_offset_s", "duration_s", "coverage", "intervals"
        }:
            raise ValueError("CPU subwindow needs its measured boundaries and coverage")
        for key in ("start_offset_s", "end_offset_s", "duration_s"):
            _numeric(cpu_window[key], key, 0, 14400)
        _numeric(cpu_window["coverage"], "CPU coverage", .8, 1)
        _numeric(cpu_window["intervals"], "CPU intervals", 1, 10**7, integer=True)
        start, end, duration = (cpu_window[k] for k in ("start_offset_s", "end_offset_s", "duration_s"))
        elapsed = metrics["elapsed_s"]
        if (elapsed is None or elapsed <= 0 or metrics["cpu_mean_percent"] is None
                or not 0 <= start < end <= elapsed
                or not math.isclose(duration, end - start, abs_tol=1e-6)
                or not math.isclose(cpu_window["coverage"], duration / elapsed, abs_tol=1e-9)):
            raise ValueError("CPU subwindow must be contiguous and contained in the native window")
    if metrics["window"] not in (None, "steady_state", "startup"):
        raise ValueError("Measurement window must be steady_state, startup or unknown")
    if result["outcome"] == "transport_aborted" and any(
        metrics[key] not in (None, 0) for key in ("source_frames", "output_frames", "encoder_latency_samples")
    ):
        raise ValueError("An aborted pre-capture transport attempt cannot contain encoded trial measurements")
    for key in ("queue_growing", "pipeline_drop_counts_complete"):
        if metrics[key] is not None and type(metrics[key]) is not bool:
            raise ValueError(f"{key} must be true, false or unknown")
    if metrics["encoder_latency_statistic"] not in (None, "max", "p95", "mean"):
        raise ValueError("Encoder latency statistic must be max, p95, mean or unknown")
    if metrics["negotiated_wire_mode"] is not None:
        metrics["negotiated_wire_mode"] = list(_mode(metrics["negotiated_wire_mode"]))
    if metrics["panel_latency_ms"] is not None:
        if metrics.get("panel_latency_method") != "physical_marker_capture":
            raise ValueError("Panel latency requires a physical marker capture measurement")
        _reference(metrics.get("panel_latency_evidence_ref"), "panel_latency_evidence_ref")
    power = metrics["whole_pi_power"]
    if power is not None:
        if not isinstance(power, dict) or set(power) != {"watts_mean", "uncertainty_watts", "scope", "meter_id", "samples"}:
            raise ValueError("Power needs watts, uncertainty, scope, meter identity and sample count")
        if power["scope"] != "whole_pi_input":
            raise ValueError("Only separate whole-Pi input measurements can support the power preset")
        _numeric(power["watts_mean"], "watts_mean", 0.01, 1000)
        _numeric(power["uncertainty_watts"], "uncertainty_watts", 0.000001, 1000)
        _numeric(power["samples"], "power samples", 1, 10**8, integer=True)
        _text(power["meter_id"], "meter_id")
    # Also catch accidental non-JSON objects in optional measurement metadata.
    try:
        json.dumps(result, allow_nan=False)
    except (ValueError, TypeError) as error:
        raise ValueError("Trial record must contain only finite JSON values") from error
    return result


def _assess(trial, context_id, workload_id, modes, policy):
    metrics, profile = trial["measurements"], trial["profile"]
    matches = trial["context_id"] == context_id and trial["workload_id"] == workload_id
    if trial["outcome"] == "transport_aborted":
        return {"trial_id": trial["trial_id"], "outcome": "transport_aborted", "blocks_candidate": False,
                "context_matches": matches, "software_passed": False, "physical_confirmed": False,
                "thermal_validated": False, "pipeline_drop_fraction": None, "encoder_headroom": None,
                "receiver_dropped_frames": None, "evidence_ref": trial["evidence_ref"],
                "reasons": [f"Transport aborted during {trial['failure_stage']}: {trial['failure_reason']}. "
                            "Capture performance was not measured."]}
    reasons = []
    if not matches:
        reasons.append("Context or workload changed; recalibrate this setup.")
    wire = (profile["wire_width"], profile["wire_height"], profile["wire_fps"])
    if wire not in modes or metrics["negotiated_wire_mode"] != list(wire):
        reasons.append("Wire mode lacks matching receiver advertisement and negotiated-mode evidence.")
    if profile["rotation"] != 0:
        reasons.append("Rotated capture remains unverified.")
    if any(value is None for value in trial["runtime_settings"].values()):
        reasons.append("Exact runtime settings are incomplete.")
    if trial["runtime_settings"]["stock_clock"] is not True:
        reasons.append("Presets require a verified stock clock; overclock trials are separate.")
    essential = set(NUMERIC_FIELDS) - {"panel_latency_ms"}
    missing = sorted(key for key in essential if metrics[key] is None)
    if missing:
        reasons.append("Required measurements are unknown: " + ", ".join(missing))
    duration = metrics["elapsed_s"]
    if metrics["window"] != "steady_state" or metrics["warmup_s"] is None or metrics["warmup_s"] < policy.min_warmup_s:
        reasons.append("Use a measured steady-state window after the warmup interval.")
    if duration is not None and duration < policy.min_trial_s:
        reasons.append(f"At least {policy.min_trial_s:g} measured seconds are required.")
    if metrics["temperature_peak_c"] is not None and metrics["temperature_peak_c"] >= policy.max_temperature_c:
        reasons.append("Temperature reached the calibration backoff limit.")
    before, seen = metrics["throttled_bits_before"], metrics["throttled_bits_seen"]
    if before is not None and seen is not None:
        if (before | seen) & 0xF or seen & ~before & 0xF0000:
            reasons.append("Active or new undervoltage, capping, throttling or thermal flags were observed.")
        if (before | seen) & ~0xF000F:
            reasons.append("Unrecognized health flags require diagnosis.")
    if metrics["decode_failures"] not in (None, 0) or metrics["disconnects"] not in (None, 0):
        reasons.append("The trial had a decode failure or stream disconnect.")
    if metrics["queue_growing"] is not False:
        reasons.append("A stable pre-encoder queue has not been demonstrated.")
    if metrics["pipeline_drop_counts_complete"] is not True:
        reasons.append("Pipeline drop observation is incomplete.")
    inputs, outputs, drops = (metrics[key] for key in ("source_frames", "output_frames", "pipeline_dropped_frames"))
    fraction = drops / inputs if drops is not None and inputs else None
    if fraction is not None and fraction >= policy.max_pipeline_drop_fraction:
        reasons.append("Observed pipeline drops reached the admission limit.")
    if duration and inputs is not None and outputs is not None:
        ratios = (inputs / (duration * profile["content_fps"]), outputs / (duration * profile["wire_fps"]))
        if any(not policy.min_frame_ratio <= ratio <= policy.max_frame_ratio for ratio in ratios):
            reasons.append("Measured source or encoded-frame cadence does not match the requested rates.")
    if duration and metrics["health_samples"] is not None:
        if metrics["health_samples"] < math.ceil(duration / policy.max_health_gap_s):
            reasons.append("Health sample coverage is insufficient.")
    if metrics["health_max_gap_s"] is not None and metrics["health_max_gap_s"] > policy.max_health_gap_s:
        reasons.append("The health signal was absent for too long.")
    latency, samples = metrics["encoder_latency_ms"], metrics["encoder_latency_samples"]
    if metrics["encoder_latency_statistic"] not in ("max", "p95"):
        reasons.append("A measured encoder maximum or p95 is required; a mean is insufficient.")
    if samples is not None and outputs is not None and samples < max(30, outputs * 0.9):
        reasons.append("Encoder timing coverage is insufficient.")
    headroom = 1 - latency / (1000 / profile["wire_fps"]) if latency is not None else None
    if headroom is not None and headroom < policy.min_encoder_headroom:
        reasons.append("Encoder timing does not retain the required stream-rate headroom.")
    physical_failed = any(value is False for value in trial["observations"].values())
    if physical_failed:
        reasons.append("The recorded physical readability, geometry or motion check failed.")
    physical = all(value is True for value in trial["observations"].values())
    return {
        "trial_id": trial["trial_id"], "outcome": "completed", "blocks_candidate": matches and bool(reasons),
        "context_matches": matches, "software_passed": not reasons,
        "reasons": reasons, "physical_confirmed": physical,
        "thermal_validated": not reasons and duration >= policy.thermal_validation_s,
        "pipeline_drop_fraction": fraction, "encoder_headroom": headroom,
        "receiver_dropped_frames": None, "evidence_ref": trial["evidence_ref"],
    }


def _candidate_key(trial):
    return json.dumps([trial["profile"], trial["runtime_settings"]], sort_keys=True)


def _quality(group):
    profile = group[0]["profile"]
    # Scaling an image into a larger signal cannot create additional source detail.
    detail = min(profile["source_width"] * profile["source_height"],
                 profile["wire_width"] * profile["wire_height"])
    cpu = _cpu(group)
    return detail, profile["content_fps"], -cpu, profile["bitrate_kbps"]


def _cpu(group):
    samples = []
    for item in group:
        metrics = item["measurements"]
        window = metrics.get("cpu_window")
        duration = window["duration_s"] if window is not None else metrics["elapsed_s"]
        samples.append((metrics["cpu_mean_percent"], duration))
    return sum(cpu * duration for cpu, duration in samples) / sum(duration for _, duration in samples)


def _power_summary(group):
    powers = [item["measurements"]["whole_pi_power"] for item in group
              if item["measurements"]["whole_pi_power"] is not None]
    if len(powers) < 2 or any(item["samples"] < 10 for item in powers):
        return None
    meters = {item["meter_id"] for item in powers}
    if len(meters) != 1:
        return None
    watts = [item["watts_mean"] for item in powers]
    return {"watts_mean": statistics.mean(watts),
            "uncertainty_watts": max(max(item["uncertainty_watts"] for item in powers),
                                     (max(watts) - min(watts)) / 2),
            "meter_id": powers[0]["meter_id"], "repeats": len(powers)}


def _choose(groups, policy):
    if not groups:
        return {}, {}
    moderate = [group for group in groups if _cpu(group) <= policy.recommended_cpu_percent]
    selected = {"recommended": max(moderate, key=_quality) if moderate else None,
                "performance": max(groups, key=_quality),
                "battery_saver": min(groups, key=lambda group: (_cpu(group), tuple(-n for n in _quality(group))))}
    powers = [_power_summary(group) for group in groups]
    comparison = {"confidence": "estimated", "candidate_count": len(groups), "uncertainty_overlap": None}
    if all(power is not None for power in powers) and len({power["meter_id"] for power in powers}) == 1:
        index = min(range(len(groups)), key=lambda i: (powers[i]["watts_mean"], _cpu(groups[i])))
        best = powers[index]
        overlap = any(best["watts_mean"] + best["uncertainty_watts"] >= other["watts_mean"] - other["uncertainty_watts"]
                      for i, other in enumerate(powers) if i != index)
        selected["battery_saver"] = groups[index]
        comparison.update(best, confidence="inconclusive" if overlap else "measured", uncertainty_overlap=overlap)
    return selected, comparison


def _unavailable(reason):
    return {"available": False, "reason": reason, "settings": None, "measurement": None,
            "runtime_settings": None, "trial_ids": [], "confidence": {}, "power_comparison": None}


def _preset(key, group, assessment_map, comparison, *, provisional=False):
    if group is None:
        return _unavailable("No tested candidate meets the moderate CPU-load criterion.")
    representative = max(group, key=lambda item: item["measurements"]["elapsed_s"])
    metrics = representative["measurements"]
    physical = any(assessment_map[item["trial_id"]]["physical_confirmed"] for item in group)
    thermal = any(assessment_map[item["trial_id"]]["thermal_validated"] for item in group)
    missing = []
    if not thermal:
        missing.append("20-minute thermal validation")
    if not physical:
        missing.append("physical readability, geometry and motion confirmation")
    cpu = _cpu(group)
    summary = f"Measured sender pipeline: {cpu:.1f}% mean CPU; peak {max(item['measurements']['temperature_peak_c'] for item in group):g} °C. "
    if key == "battery_saver":
        confidence = comparison["confidence"]
        if confidence == "measured":
            summary += (f"Lowest measured whole-Pi input power among {comparison['candidate_count']} qualified tested candidate(s): "
                        f"{comparison['watts_mean']:.2f} ± {comparison['uncertainty_watts']:.2f} W. ")
        elif confidence == "inconclusive":
            summary += "Whole-Pi power was measured, but candidate uncertainty intervals overlap. "
        else:
            summary += "Battery Saver is estimated from measured CPU load; whole-Pi power was not compared across every candidate. "
    else:
        confidence = "measured" if metrics["whole_pi_power"] else "unmeasured"
    latency_observed = any(item["measurements"]["panel_latency_ms"] is not None for item in group)
    if not latency_observed:
        summary += "Physical panel latency is unmeasured."
    return {
        "available": not missing, "reason": "Needs " + " and ".join(missing) + "." if missing else "",
        "settings": copy.deepcopy(representative["profile"]),
        "runtime_settings": copy.deepcopy(representative["runtime_settings"]),
        "measurement": summary.strip(), "trial_ids": sorted(item["trial_id"] for item in group),
        "confidence": {"selection": "provisional" if provisional else "qualified",
                       "physical_observation": "recorded" if physical else "missing",
                       "thermal_validation": "recorded" if thermal else "missing", "power": confidence,
                       "panel_latency": "physically_measured" if latency_observed else "unmeasured",
                       "native_panel_resolution": "unknown", "receiver_delivery": "unmeasured"},
        "power_comparison": copy.deepcopy(comparison) if key == "battery_saver" else None,
    }


def select_presets(trials, *, context_id, workload_id, advertised_modes, policy=None):
    """Evaluate a bounded trial set and return records, shortlist and Session1 presets.

    No I/O, hardware access, trial generation, preset application or persistence.
    context_id must change with any material hardware/software/settings change.
    """
    policy = CalibrationPolicy() if policy is None else policy
    if not isinstance(policy, CalibrationPolicy):
        raise ValueError("A validated calibration policy is required")
    _text(context_id, "context_id")
    _text(workload_id, "workload_id")
    if not isinstance(trials, (list, tuple)) or len(trials) > policy.max_trials:
        raise ValueError("Calibration trials must be a bounded list")
    if not isinstance(advertised_modes, (list, tuple)) or len(advertised_modes) > 32:
        raise ValueError("Receiver modes must be a bounded list")
    modes = {_mode(mode) for mode in advertised_modes}
    records = [record_trial(item) for item in trials]
    if len({item["trial_id"] for item in records}) != len(records):
        raise ValueError("Duplicate trial IDs cannot establish independent repeats")
    grouped = {}
    for record in records:
        grouped.setdefault(_candidate_key(record), []).append(record)
    if len(grouped) > policy.max_candidates:
        raise ValueError("Too many calibration candidates")
    assessments = [_assess(item, context_id, workload_id, modes, policy) for item in records]
    assessment_map = {item["trial_id"]: item for item in assessments}
    passing = []
    for group in grouped.values():
        group = [item for item in group if item["outcome"] == "completed"
                 and assessment_map[item["trial_id"]]["context_matches"]]
        # A failed trial for these exact settings prevents promotion; hiding it
        # behind another good repeat must not turn an unstable candidate green.
        if group and all(assessment_map[item["trial_id"]]["software_passed"] for item in group):
            passing.append(group)
    qualified = [group for group in passing
                 if any(assessment_map[item["trial_id"]]["thermal_validated"] for item in group)
                 and any(assessment_map[item["trial_id"]]["physical_confirmed"] for item in group)]
    provisional, provisional_power = _choose(passing, policy)
    selected, power = _choose(qualified, policy)
    shortlist, presets = {}, {}
    for key in PRESET_NAMES:
        if key in provisional:
            shortlist[key] = _preset(key, provisional[key], assessment_map, provisional_power, provisional=True)
        else:
            shortlist[key] = _unavailable("No candidate passed the measured software and safety criteria.")
        if key in selected:
            presets[key] = _preset(key, selected[key], assessment_map, power)
        else:
            presets[key] = _unavailable(shortlist[key]["reason"] or "Physical and thermal validation is still required.")
    return {"api_version": 1, "context_id": context_id, "workload_id": workload_id,
            "policy": asdict(policy), "advertised_modes": [list(mode) for mode in sorted(modes)],
            "records": records, "assessments": assessments, "shortlist": shortlist, "presets": presets}
