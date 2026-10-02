"""Bounded, pure accumulation of producer evidence into calibration API v1.

The caller owns STREAMING verification, producer timestamps on one monotonic
clock, periodic check_deadline(), raw evidence storage and stopping hardware.
This object owns no I/O, timers or processes. It never chooses a preset.
"""

from __future__ import annotations

import copy
import math

from .calibration import CalibrationPolicy, NUMERIC_FIELDS, record_trial
from .health import safety_action


_DROPS = ("videorate_frames_dropped", "appsrc_frames_dropped", "other_pipeline_frames_dropped")
_TIMING_COUNTERS = ("matched", "invalid", "unmatched", "tracking_evicted", "discontinuities")
_NATIVE_COUNTERS = ("raw_frames_pushed", "encoded_frames",
                    "encoded_buffers_with_unknown_frame_count", *_DROPS,
                    "videorate_frames_duplicated")
_MAX_SAMPLES = 32768  # Combined native + health rows, enough for four hours at 1 Hz each.
_MAX_RUN_US = 14400 * 1_000_000
_COHORT_BOUNDARIES = ("appsrc", "source_to_rate", "rate_to_scale", "scale",
                      "scale_to_convert", "convert", "convert_to_queue", "queue", "queue_to_encoder")
_COHORT_COUNTS = ("accepted_source_frames", "rejected_source_frames", "emitted_wire_frames",
                  "rate_source_frames_dropped", "rate_frames_duplicated")
_COHORT_TIMES = ("streaming_us", "epoch", "started_us", "ended_us", "closed_us")
_COHORT_PENDING = {"cohort-not-started", "source-window-not-ended", "encoder-sink-eos-pending",
                   "rate-ledger-unsettled", "boundary-ledger-unsettled", "required-probe-unavailable"}
_COHORT_FIELDS = {"version", "enabled", "scope", "complete", "closed", "requested_warmup_us",
                  "requested_duration_us", "invalid_reason", "source_seq_first", "source_seq_end_exclusive",
                  "encoder_src_eos_seen", "encoded_frames", "encoder_output_key_unknown",
                  "encoder_sink_to_src_us", "boundary_frames_dropped", "pipeline_frames_dropped",
                  *_COHORT_COUNTS, *_COHORT_TIMES}
_COHORT_TIMING_COUNTS = ("entered", "exited", *_TIMING_COUNTERS)


def _number(value, low=0, high=10**12, *, integer=False):
    valid = type(value) is int if integer else type(value) in (int, float)
    return value if valid and low <= value <= high and math.isfinite(value) else None


def _time(value):
    if _number(value, high=2**63 - 1, integer=True) is None:
        raise ValueError("monotonic timestamps must be nonnegative integer microseconds")
    return value


def _empty_measurements():
    return {**dict.fromkeys(NUMERIC_FIELDS), "window": None, "cpu_window": None,
            "pipeline_drop_counts_complete": False, "encoder_latency_statistic": None,
            "queue_growing": None, "negotiated_wire_mode": None, "whole_pi_power": None}


def _trial(trial, mode=None):
    if not isinstance(trial, dict) or set(trial) != {
        "trial_id", "context_id", "workload_id", "observed_at", "evidence_ref",
        "profile", "runtime_settings",
    }:
        raise ValueError("Supply trial identity, profile and exact observed runtime metadata only")
    metrics = _empty_measurements()
    metrics["negotiated_wire_mode"] = mode
    return record_trial({**trial, "measurements": metrics,
                         "observations": dict.fromkeys(("readable", "geometry_correct",
                                                        "motion_acceptable"))})


def record_transport_abort(trial, *, stage, reason):
    """Record a pre-capture failure, with all capture measurements unknown."""
    result = _trial(trial)
    result.update(outcome="transport_aborted", failure_stage=stage, failure_reason=reason)
    return record_trial(result)


def _cohort(sample):
    """Validate the native v1 wire shape; retain no arbitrary producer text.

    This checks internal consistency, not authenticity. The caller still owns
    direct producer capture and the private raw receipt.
    """
    if "local_frame_accounting" not in sample:
        return None, None
    c = sample["local_frame_accounting"]
    if (not isinstance(c, dict) or set(c) != _COHORT_FIELDS
            or type(c.get("version")) is not int or c["version"] != 1
            or c.get("scope") != "accepted-source-to-encoder-sink"):
        return None, "cohort_shape"
    flags = ("enabled", "complete", "closed", "encoder_src_eos_seen", "encoder_output_key_unknown")
    if any(type(c[key]) is not bool for key in flags):
        return None, "cohort_shape"
    if not c["enabled"]:
        return (None, "cohort_shape") if c["complete"] or c["closed"] else (None, None)
    for key in (*_COHORT_COUNTS, "requested_warmup_us", "requested_duration_us"):
        if _number(c[key], high=_MAX_RUN_US if key.startswith("requested") else 10**8, integer=True) is None:
            return None, "cohort_shape"
    for key in (*_COHORT_TIMES, "source_seq_first", "source_seq_end_exclusive"):
        if c[key] is not None and _number(c[key], high=2**63-1, integer=True) is None:
            return None, "cohort_shape"
    for key in ("encoded_frames", "pipeline_frames_dropped"):
        if c[key] is not None and _number(c[key], high=10**8, integer=True) is None:
            return None, "cohort_shape"
    drops = c["boundary_frames_dropped"]
    if (not isinstance(drops, dict) or set(drops) != set(_COHORT_BOUNDARIES)
            or any(_number(value, high=10**8, integer=True) is None for value in drops.values())):
        return None, "cohort_shape"
    timing = c["encoder_sink_to_src_us"]
    if (not isinstance(timing, dict) or set(timing) != {
            "available", "ambiguous", "last_us", "max_us", "mean_us", "oldest_pending_age_us",
            *_COHORT_TIMING_COUNTS}
            or type(timing["available"]) is not bool or type(timing["ambiguous"]) is not bool
            or any(_number(timing[key], high=10**8, integer=True) is None for key in _COHORT_TIMING_COUNTS)):
        return None, "cohort_shape"
    for key in ("last_us", "max_us", "mean_us", "oldest_pending_age_us"):
        if timing[key] is not None and _number(timing[key], high=_MAX_RUN_US,
                                             integer=key != "mean_us") is None:
            return None, "cohort_shape"
    reason = c["invalid_reason"]
    if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason) <= 96):
        return None, "cohort_shape"
    if c["complete"] and (not c["closed"] or reason is not None):
        return None, "cohort_closure_invalid"
    if not c["complete"] and reason is None:
        return None, "cohort_closure_invalid"
    if c["encoder_src_eos_seen"] and not c["closed"]:
        return None, "cohort_closure_invalid"
    if c["closed"] != (c["closed_us"] is not None):
        return None, "cohort_closure_invalid"
    if c["complete"]:
        accepted, rejected, emitted = (c[key] for key in _COHORT_COUNTS[:3])
        source_at_rate = accepted - drops["appsrc"] - drops["source_to_rate"]
        surviving_source = source_at_rate - c["rate_source_frames_dropped"]
        total = sum(drops.values()) + c["rate_source_frames_dropped"]
        first, end = c["source_seq_first"], c["source_seq_end_exclusive"]
        if (accepted == 0 or rejected or first is None or end is None or end - first != accepted
                or surviving_source < 0 or (surviving_source == 0 and c["rate_frames_duplicated"])
                or c["pipeline_frames_dropped"] != total
                or accepted + c["rate_frames_duplicated"] != emitted + total
                or timing["entered"] != emitted):
            return None, "cohort_counts_invalid"
    elif c["pipeline_frames_dropped"] is not None:
        return None, "cohort_counts_invalid"
    if (timing["matched"] > timing["entered"] or timing["matched"] > timing["exited"]
            or timing["unmatched"] + timing["matched"] > timing["exited"]):
        return None, "cohort_timing_invalid"
    if timing["matched"]:
        if (any(timing[key] is None for key in ("last_us", "max_us", "mean_us"))
                or timing["last_us"] > timing["max_us"] or timing["mean_us"] > timing["max_us"]):
            return None, "cohort_timing_invalid"
    elif any(timing[key] is not None for key in ("last_us", "max_us", "mean_us")):
        return None, "cohort_timing_invalid"
    encoded = c["encoded_frames"]
    if encoded is not None and (encoded != timing["matched"] or encoded > c["emitted_wire_frames"]
            or c["encoder_output_key_unknown"] or timing["ambiguous"] or timing["invalid"] or timing["tracking_evicted"]):
        return None, "cohort_timing_invalid"
    result = copy.deepcopy(c)
    if reason is not None and reason not in _COHORT_PENDING:
        result["invalid_reason"] = "native-reported-invalid"
    return result, None


def _native(sample):
    """Retain fixed scalar fields only, never arbitrary producer strings/frames."""
    row = {key: _number(sample.get(key), integer=True) for key in _NATIVE_COUNTERS}
    row.update(monotonic_us=_time(sample.get("monotonic_us")),
               elapsed_us=_number(sample.get("elapsed_us"), integer=True),
               source_probe_available=sample.get("source_probe_available") is True,
               encoder_probes_available=sample.get("encoder_probes_available") is True)
    timing = sample.get("encoder_sink_to_src_us")
    timing = timing if isinstance(timing, dict) else {}
    row["timing"] = {key: _number(timing.get(key), integer=True) for key in _TIMING_COUNTERS}
    row["timing"].update(available=timing.get("available") is True,
                         ambiguous=timing.get("ambiguous") is not False,
                         max_us=_number(timing.get("max_us"), high=14400000 * 1000))
    queue = sample.get("pre_encoder_queue_residence_us")
    queue = queue if isinstance(queue, dict) else {}
    row["queue"] = {
        "buffers": _number(sample.get("pre_encoder_queue_buffers"), integer=True),
        "media_duration_ns": _number(sample.get("pre_encoder_queue_media_duration_ns"),
                                     high=10**18, integer=True),
        "available": queue.get("available") is True,
        "oldest_pending_age_us": _number(queue.get("oldest_pending_age_us"), integer=True),
    }
    row["cohort"], row["cohort_error"] = _cohort(sample)
    row["final"] = sample["final"]
    return row


class MeasurementAccumulator:
    """One immutable stream identity, one warmup and one measured window.

    ``trial`` contains identity, profile and runtime_settings only. The baseline
    health sample is taken before warmup, at most one health-gap limit before
    ``streaming_us``. Health samples add ``monotonic_us`` to HealthMonitor's
    fields; CPU intervals carry their own actual counter-read timestamps.
    Native samples are direct pipeline-telemetry events, never GUI poll times.

    A validated closed source cohort supplies its own start/end boundaries;
    drain time is separate. Older producers use the first usable native snapshot
    after warmup and the last non-final snapshot, with loss completeness unknown.
    CPU uses contiguous complete counter intervals inside the selected window,
    covering at least 80 percent, with its bounds retained. No warmup CPU is
    silently apportioned.
    ``finish`` returns {record, diagnostics...}; persist the whole result so
    reasons survive the existing record schema. A stop invalidates its window.
    """

    def __init__(self, *, trial, stream_id, streaming_us, baseline_health,
                 negotiated_wire_mode=None, thermal=False, policy=None, max_samples=_MAX_SAMPLES):
        if (not isinstance(stream_id, str) or not 1 <= len(stream_id) <= 160
                or type(thermal) is not bool or type(max_samples) is not int
                or not 16 <= max_samples <= _MAX_SAMPLES):
            raise ValueError("Invalid stream identity, thermal flag or sample bound")
        if policy is not None and not isinstance(policy, CalibrationPolicy):
            raise ValueError("policy must be a CalibrationPolicy")
        self.policy = policy or CalibrationPolicy()
        self._record = _trial(trial, negotiated_wire_mode)
        self.stream_id = stream_id
        self.streaming_us = _time(streaming_us)
        self.thermal = thermal
        self.max_samples = max_samples
        self._native_rows = {}
        self._health_rows = {}
        self._stops = {}
        self._failures = set()
        self._start_us = None
        self._last_check_us = self.streaming_us
        self._finished = False
        self._final_seen = False
        self._cohort_seen = False
        self._cohort_bad = False
        self._cohort_first_us = None
        self._cohort_latest = None
        self._cohort_latest_us = None
        self._seen_bits = 0
        self._bits_unknown = False
        if not isinstance(baseline_health, dict):
            raise ValueError("A timestamped pre-warmup health baseline is required")
        baseline_us = _time(baseline_health.get("monotonic_us"))
        if not 0 <= self.streaming_us - baseline_us <= self.policy.max_health_gap_s * 1_000_000:
            raise ValueError("Health baseline must immediately precede streaming/warmup")
        self._baseline_bits = _number(baseline_health.get("throttled_bits"),
                                      high=0xFFFFFFFF, integer=True)
        self.add_health(baseline_health)

    @property
    def should_stop(self):
        return bool(self._stops)

    def _open(self):
        if self._finished:
            raise RuntimeError("Measurement already finished")

    def _stop(self, reason, at_us):
        self._stops.setdefault(reason, at_us)
        if reason.startswith(("native_", "timing_")) or reason in ("stream_changed", "sample_limit"):
            self._cohort_bad = True

    def _remember(self, rows, row, kind):
        at_us = row["monotonic_us"]
        if at_us in rows:
            if rows[at_us] != row:
                self._stop(f"{kind}_conflicting_duplicate", at_us)
            return False
        if rows and at_us < next(reversed(rows)):
            self._stop(f"{kind}_backwards", at_us)
            return False
        if len(self._native_rows) + len(self._health_rows) >= self.max_samples:
            self._stop("sample_limit", at_us)
            return False
        if rows and at_us - next(reversed(rows)) > self.policy.max_health_gap_s * 1_000_000:
            self._stop(f"{kind}_gap", at_us)
        if at_us - self.streaming_us > _MAX_RUN_US:
            self._stop("run_deadline", at_us)
            return False
        rows[at_us] = row
        return True

    def add_health(self, sample):
        self._open()
        if not isinstance(sample, dict):
            raise ValueError("Health sample must be a timestamped object")
        row = {"monotonic_us": _time(sample.get("monotonic_us")),
               "temperature_c": _number(sample.get("temperature_c"), -20, 150),
               "cpu_percent": _number(sample.get("cpu_percent"), 0, 100),
               "throttled_bits": _number(sample.get("throttled_bits"), high=0xFFFFFFFF,
                                          integer=True)}
        interval = sample.get("cpu_interval")
        row["cpu_interval"] = None
        if isinstance(interval, dict) and set(interval) == {"start_us", "end_us"}:
            start, end = (_number(interval[key], high=2**63 - 1, integer=True)
                          for key in ("start_us", "end_us"))
            if (start is not None and end is not None and 0 <= start < end <= row["monotonic_us"]
                    and end - start <= self.policy.max_health_gap_s * 1_000_000):
                row["cpu_interval"] = {"start_us": start, "end_us": end}
        if not self._remember(self._health_rows, row, "health"):
            return False
        at_us, bits = row["monotonic_us"], row["throttled_bits"]
        action = safety_action(row)
        if action != "ok":
            self._stop(f"health_{action}", at_us)
        if row["temperature_c"] is not None and row["temperature_c"] >= self.policy.max_temperature_c:
            self._stop("calibration_temperature_limit", at_us)
        if bits is None:
            self._bits_unknown = True
        else:
            self._seen_bits |= bits
            if self._baseline_bits is not None and bits & ~self._baseline_bits & 0xF0000:
                self._stop("new_sticky_fault", at_us)
        return True

    def add_native(self, stream_id, sample):
        self._open()
        if not isinstance(sample, dict):
            raise ValueError("Native sample must be a timestamped object")
        at_us = _time(sample.get("monotonic_us"))
        if stream_id != self.stream_id:
            self._stop("stream_changed", at_us)
            self._cohort_bad = True
            return False
        if (type(sample.get("metrics_version")) is not int or sample["metrics_version"] != 1
                or sample.get("event") != "pipeline-telemetry"
                or type(sample.get("final")) is not bool):
            self._stop("native_version_or_shape", at_us)
            return False
        if at_us < self.streaming_us:
            self._stop("native_before_streaming", at_us)
            return False
        if self._final_seen and not sample["final"]:
            self._stop("native_after_teardown", at_us)
            self._cohort_bad = True
            return False
        row = _native(sample)
        if sample["final"]:
            self._final_seen = True
            # Old cumulative teardown counters are still excluded. A new
            # explicitly closed cohort carries its earlier source boundaries.
            if self._cohort_seen or row["cohort"] is not None or row["cohort_error"]:
                if self._remember(self._native_rows, row, "native"):
                    self._accept_cohort(row)
            return False
        previous = next(reversed(self._native_rows.values()), None)
        if not self._remember(self._native_rows, row, "native"):
            return False
        if previous:
            for key in (*_NATIVE_COUNTERS, "elapsed_us"):
                if row[key] is not None and previous[key] is not None and row[key] < previous[key]:
                    self._stop("native_counter_reset", at_us)
            for key in _TIMING_COUNTERS:
                old, new = previous["timing"][key], row["timing"][key]
                if old is not None and new is not None and new < old:
                    self._stop("timing_counter_reset", at_us)
        elif at_us - self.streaming_us > self.policy.max_health_gap_s * 1_000_000:
            self._stop("native_gap", at_us)
        usable = row["source_probe_available"] and row["encoder_probes_available"]
        if self._start_us is not None and not usable:
            self._stop("native_probe_lost", at_us)
        if (self._start_us is None and usable
                and at_us - self.streaming_us >= self.policy.min_warmup_s * 1_000_000):
            self._start_us = at_us
        self._accept_cohort(row)
        return True

    def _accept_cohort(self, row):
        at_us, current = row["monotonic_us"], row["cohort"]

        def reject(reason):
            self._cohort_bad = True
            self._stop(reason, at_us)

        if row["cohort_error"]:
            self._cohort_seen = True
            reject(row["cohort_error"])
            return
        if current is None:
            if self._cohort_seen:
                reject("cohort_missing")
            return
        self._cohort_seen = True
        if self._cohort_first_us is None:
            self._cohort_first_us = at_us
        if current["complete"] and not (row["source_probe_available"] and row["encoder_probes_available"]):
            reject("cohort_probe_unavailable")
        warmup, duration = current["requested_warmup_us"], current["requested_duration_us"]
        if (not self.policy.min_warmup_s * 1e6 <= warmup <= 60_000_000
                or not self.policy.min_trial_s * 1e6 <= duration <= 14_335_000_000
                or warmup + duration > _MAX_RUN_US - 5_000_000):
            reject("cohort_time_bounds")
        streaming, start, end, closed = (current[key] for key in
                                         ("streaming_us", "started_us", "ended_us", "closed_us"))
        first, last = current["source_seq_first"], current["source_seq_end_exclusive"]
        if streaming is not None and not self.streaming_us <= streaming <= min(
                at_us, self.streaming_us + self.policy.max_health_gap_s * 1e6):
            reject("cohort_streaming_mismatch")
        if start is None:
            if (current["epoch"] is not None or first is not None or end is not None
                    or any(current[key] for key in _COHORT_COUNTS)):
                reject("cohort_window_invalid")
        elif (streaming is None or current["epoch"] != start or first is None
              or start <= 0 or not streaming + warmup <= start <= at_us
              or self._cohort_first_us > start + self.policy.max_health_gap_s * 1e6):
            reject("cohort_window_invalid")
        if (end is None) != (last is None):
            reject("cohort_window_invalid")
        if end is not None and (start is None or not start + duration <= end <= at_us
                                or first is None or last < first):
            reject("cohort_window_invalid")
        if closed is not None and (end is None or not end <= closed <= at_us
                                    or closed - end > 5_000_000 or streaming is None
                                    or closed - streaming > warmup + duration + 5_000_000):
            reject("cohort_window_invalid")
        if current["complete"] and (start is None or end is None or closed is None):
            reject("cohort_window_invalid")
        previous = self._cohort_latest
        if previous:
            for key in ("streaming_us", "started_us", "ended_us", "closed_us"):
                if (previous[key] is None and current[key] is not None
                        and current[key] < self._cohort_latest_us):
                    reject("cohort_boundary_backdated")
            fixed = ("requested_warmup_us", "requested_duration_us", *_COHORT_TIMES,
                     "source_seq_first", "source_seq_end_exclusive")
            if any(previous[key] is not None and current[key] != previous[key] for key in fixed):
                reject("cohort_identity_changed")
            for flag in ("complete", "closed", "encoder_src_eos_seen", "encoder_output_key_unknown"):
                if previous[flag] and not current[flag]:
                    reject("cohort_state_reversed")
            if any(current[key] < previous[key] for key in _COHORT_COUNTS):
                reject("cohort_counter_reset")
            if any(current["boundary_frames_dropped"][key] < previous["boundary_frames_dropped"][key]
                   for key in _COHORT_BOUNDARIES):
                reject("cohort_counter_reset")
            old_timing, new_timing = previous["encoder_sink_to_src_us"], current["encoder_sink_to_src_us"]
            for key in (*_COHORT_TIMING_COUNTS, "max_us"):
                if (old_timing[key] is not None and new_timing[key] is not None
                        and new_timing[key] < old_timing[key]):
                    reject("cohort_timing_reset")
            if previous["encoder_src_eos_seen"]:
                # Pending age may continue to grow for an encoder input that
                # never produced output; all closed population counts are fixed.
                old_fixed, new_fixed = copy.deepcopy(previous), copy.deepcopy(current)
                for item in (old_fixed, new_fixed):
                    item["encoder_sink_to_src_us"].pop("oldest_pending_age_us")
                if old_fixed != new_fixed:
                    reject("cohort_changed_after_eos")
        if current["invalid_reason"] == "native-reported-invalid":
            reject("cohort_reported_invalid")
        self._cohort_latest = current
        self._cohort_latest_us = at_us

    def check_deadline(self, at_us):
        """Caller must invoke periodically even when neither producer responds."""
        self._open()
        at_us = _time(at_us)
        if at_us < self.streaming_us:
            raise ValueError("Deadline check precedes streaming")
        if at_us < self._last_check_us:
            raise ValueError("Deadline clock went backwards")
        if at_us - self.streaming_us > _MAX_RUN_US:
            self._stop("run_deadline", at_us)
        for kind, rows in (("health", self._health_rows), ("native", self._native_rows)):
            last = next(reversed(rows), self.streaming_us)
            if at_us < last:
                raise ValueError("Deadline check precedes a producer sample")
            if at_us - last > self.policy.max_health_gap_s * 1_000_000:
                self._stop(f"{kind}_gap", at_us)
        self._last_check_us = at_us
        return self.should_stop

    @staticmethod
    def _delta(rows, key):
        values = [row[key] for row in rows]
        if not values or any(value is None for value in values):
            return None
        if any(after < before for before, after in zip(values, values[1:])):
            return None
        return values[-1] - values[0]

    def _health_metrics(self, start, end):
        rows = list(self._health_rows.values())
        inside = [row for row in rows if start <= row["monotonic_us"] <= end]
        valid = [row for row in inside if row["temperature_c"] is not None
                 and row["throttled_bits"] is not None and not row["throttled_bits"] & ~0xF000F]
        boundaries = [start, *(row["monotonic_us"] for row in valid), end]
        gap = max((b - a for a, b in zip(boundaries, boundaries[1:])), default=0) / 1_000_000
        intervals = [row for row in rows if row["cpu_interval"] is not None
                     and start <= row["cpu_interval"]["start_us"] < row["cpu_interval"]["end_us"] <= end]
        contiguous = all(a["cpu_interval"]["end_us"] == b["cpu_interval"]["start_us"]
                         for a, b in zip(intervals, intervals[1:]))
        missing = any(row["cpu_percent"] is None or row["cpu_interval"] is None for row in inside)
        covered = sum(row["cpu_interval"]["end_us"] - row["cpu_interval"]["start_us"]
                      for row in intervals)
        usable = (bool(intervals) and contiguous and not missing and end > start
                  and covered >= .8 * (end - start)
                  and all(row["cpu_percent"] is not None for row in intervals))
        cpu = window = None
        if usable:
            cpu = sum((row["cpu_interval"]["end_us"] - row["cpu_interval"]["start_us"])
                      * row["cpu_percent"] for row in intervals) / covered
            window = {"start_offset_s": (intervals[0]["cpu_interval"]["start_us"] - start) / 1e6,
                      "end_offset_s": (intervals[-1]["cpu_interval"]["end_us"] - start) / 1e6,
                      "duration_s": covered / 1e6, "coverage": covered / (end - start),
                      "intervals": len(intervals)}
        if cpu is None:
            self._failures.add("cpu_coverage_unknown")
        temperature = None
        if inside and all(row["temperature_c"] is not None for row in inside):
            temperature = max(row["temperature_c"] for row in inside)
        return {"cpu_mean_percent": cpu, "cpu_window": window, "temperature_peak_c": temperature,
                "health_samples": len(valid), "health_max_gap_s": gap}

    def _native_metrics(self, rows):
        metrics = {}
        metrics["source_frames"] = self._delta(rows, "raw_frames_pushed") if all(
            row["source_probe_available"] for row in rows) else None
        metrics["output_frames"] = self._delta(rows, "encoded_frames") if all(
            row["encoder_probes_available"] and row["encoded_buffers_with_unknown_frame_count"] == 0
            for row in rows) else None
        timing = [row["timing"] for row in rows]
        trustworthy = all(row["available"] and not row["ambiguous"] for row in timing)
        trustworthy &= all(self._delta(timing, key) == 0 for key in _TIMING_COUNTERS if key != "matched")
        matched = self._delta(timing, "matched")
        maxima = [row["max_us"] for row in timing]
        trustworthy &= all(value is not None for value in maxima)
        if trustworthy:
            trustworthy = all(b >= a for a, b in zip(maxima, maxima[1:]))
        if trustworthy and matched is not None and matched > 0:
            metrics.update(encoder_latency_ms=maxima[-1] / 1000,
                           encoder_latency_statistic="max", encoder_latency_samples=matched)
        else:
            self._failures.add("encoder_timing_unknown")
        return metrics

    def _cohort_metrics(self, cohort):
        metrics = {"source_frames": cohort["accepted_source_frames"],
                   "output_frames": cohort["encoded_frames"],
                   "pipeline_dropped_frames": cohort["pipeline_frames_dropped"],
                   "pipeline_drop_counts_complete": True}
        timing = cohort["encoder_sink_to_src_us"]
        trustworthy = (timing["available"] and not timing["ambiguous"]
                       and not cohort["encoder_output_key_unknown"]
                       and cohort["encoded_frames"] is not None and timing["matched"] > 0
                       and timing["matched"] == timing["entered"] == timing["exited"]
                       and timing["oldest_pending_age_us"] is None
                       and all(timing[key] == 0 for key in _TIMING_COUNTERS if key != "matched"))
        if trustworthy:
            metrics.update(encoder_latency_ms=timing["max_us"] / 1000,
                           encoder_latency_statistic="max", encoder_latency_samples=timing["matched"])
        else:
            self._failures.add("encoder_timing_unknown")
        return metrics

    def finish(self, at_us, *, observations=None, whole_pi_power=None, panel_latency=None):
        """Freeze a schema-valid partial record plus bounded diagnostic receipts.

        Optional physical/meter values must be independently observed for this
        exact trial. Loss completeness comes only from a validated closed native
        cohort. No caller decoder/drop/queue-stability override is accepted.
        """
        self.check_deadline(at_us)
        result = copy.deepcopy(self._record)
        metrics = result["measurements"]
        cohort = self._cohort_latest
        use_cohort = (not self._cohort_bad and cohort is not None and cohort["complete"]
                      and cohort["closed"] and cohort["encoder_src_eos_seen"])
        native_rows = [row for row in self._native_rows.values() if not row["final"]]
        start = cohort["started_us"] if use_cohort else self._start_us
        end = (cohort["ended_us"] if use_cohort else
               native_rows[-1]["monotonic_us"] if start is not None and native_rows else None)
        duration = (end - start) / 1e6 if start is not None and end is not None else 0
        target = self.policy.thermal_validation_s if self.thermal else self.policy.min_trial_s
        if duration < target:
            self._failures.add("measurement_too_short")
        if start is not None:
            rows = [row for row in native_rows if start <= row["monotonic_us"] <= end]
            valid_window = not self.should_stop and (not self._cohort_seen or use_cohort)
            warmup_anchor = cohort["streaming_us"] if use_cohort else self.streaming_us
            metrics.update(window="steady_state" if valid_window else None,
                           warmup_s=(start - warmup_anchor) / 1e6, elapsed_s=duration)
            metrics.update(self._cohort_metrics(cohort) if use_cohort else self._native_metrics(rows))
            metrics.update(self._health_metrics(start, end))
            # Asynchronous lifetime-counter snapshots do not share cohort
            # boundaries. The exact source-defined counts live in its receipt.
            drops = dict.fromkeys(_DROPS) if use_cohort else {key: self._delta(rows, key) for key in _DROPS}
        else:
            drops = dict.fromkeys(_DROPS)
            self._failures.add("no_measured_window")
        metrics.update(throttled_bits_before=self._baseline_bits,
                       throttled_bits_seen=None if self._bits_unknown else self._seen_bits)
        self._failures.update(("decode_probe_unavailable",
                               "disconnect_coverage_unknown", "queue_trend_unknown"))
        if not metrics["pipeline_drop_counts_complete"]:
            self._failures.add("pipeline_loss_unknown")
        if self._cohort_seen and not use_cohort:
            self._failures.add("cohort_incomplete")
        for field, reason in (("source_frames", "source_count_unknown"),
                              ("output_frames", "encoded_count_unknown"),
                              ("negotiated_wire_mode", "negotiated_mode_unknown")):
            if metrics[field] is None:
                self._failures.add(reason)
        if observations is not None:
            result["observations"] = observations
        metrics["whole_pi_power"] = whole_pi_power
        if panel_latency is not None:
            if not isinstance(panel_latency, dict) or set(panel_latency) != {
                "panel_latency_ms", "panel_latency_method", "panel_latency_evidence_ref"
            }:
                raise ValueError("Panel latency requires physical marker evidence only")
            metrics.update(panel_latency)
        result = record_trial(result)
        self._finished = True
        return {"record": result, "stop_reasons": sorted(self._stops),
                "failure_reasons": sorted(self._failures),
                "first_stop_us": min(self._stops.values(), default=None),
                "window_start_us": start, "window_end_us": end,
                "cohort_drain_end_us": cohort["closed_us"] if use_cohort else None,
                "cohort_evidence_usable": use_cohort,
                "local_frame_accounting": copy.deepcopy(cohort),
                "duration_satisfied": duration >= target and (not self._cohort_seen or use_cohort),
                "thermal_duration_satisfied": duration >= self.policy.thermal_validation_s and
                                              (not self._cohort_seen or use_cohort),
                "retained_samples": len(self._native_rows) + len(self._health_rows),
                "known_drop_deltas": drops,
                "queue_observations": [{"monotonic_us": row["monotonic_us"], **row["queue"]}
                                       for row in self._native_rows.values()
                                       if start is not None and not row["final"] and
                                       start <= row["monotonic_us"] <= end]}
