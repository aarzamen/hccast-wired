# Calibration selection API v1

Pure Python module: `panelbridge.calibration`. It performs no I/O, device access,
trial generation, persistence, or profile application. The integrator owns the
bounded hardware runner and private record storage. No default record is real
calibration data.

```python
from panelbridge.calibration import CalibrationPolicy, record_trial, select_presets

validated_record = record_trial(measured_record)
result = select_presets(
    records,
    context_id=current_context_id,
    workload_id=current_workload_id,
    advertised_modes=receiver_modes,
    policy=CalibrationPolicy(),
)
# Only result["presets"] is suitable for the Session1 preset map.
# Honor each preset's available flag before enabling its action.
```

`record_trial` returns a deep copy with `api_version: 1`. Its result can be JSON
serialized and loaded through `record_trial` again. It rejects malformed values,
nonfinite numbers, unsupported profile fields, unbounded strings and absolute or
traversing evidence references. Numeric unknowns must be `None`, never zero.
Validation cannot authenticate sensor readings or replace retained raw evidence.

## Runner responsibilities

- `context_id` is an opaque fingerprint of the actual Pi/receiver pairing, OS,
  app revision, encoder build, radio/driver, output/capture setup and relevant
  lab conditions. Change it after a material change. Use private binding records;
  do not export names, addresses, credentials or serial numbers in this API.
- `workload_id` identifies the same repeatable text, pointer, monochrome motion
  and video workload. CPU-only idle samples are not substitutes for that workload.
- Supply receiver-advertised modes from actual capability evidence. The selector
  accepts only the current model's supported tuples: `(1280,720,30)` and
  `(1920,1080,30)`. A supported `Profile` does not prove receiver support. Pass
  only modes actually advertised by this receiver; an empty list admits none.
- Save raw evidence and actual start/end boundaries privately. Reference it with
  a relative `evidence_ref`. Trial IDs must identify independent attempts; never
  relabel one run to create the repeated power evidence required below.
- Settings in one candidate are the exact complete `Profile` plus complete
  `runtime_settings`. Change either and it is a different candidate. The runner
  must confirm it can restore these runtime settings before applying a preset;
  the selector does not add CPU or Wi-Fi controls to the controller.

## Trial record

Required top-level fields:

| Field | Meaning |
|---|---|
| `trial_id` | Unique bounded trial identifier |
| `context_id`, `workload_id` | Actual setup and workload fingerprints |
| `observed_at` | ISO timestamp with timezone for the measured attempt |
| `evidence_ref` | Relative reference to its private raw evidence |
| `profile` | Existing complete `Profile.to_dict()` record or a `Profile` object |
| `runtime_settings` | Complete dictionary below |
| `measurements` | Complete dictionary below; unknown values are null |
| `observations` | `readable`, `geometry_correct`, `motion_acceptable`: each true, false or null |

`outcome` defaults to `completed`, which means the measurement record is finished,
not that it passed. A pre-capture failure uses `outcome: "transport_aborted"`,
`failure_stage: "discovery"`, `"association"` or `"rtsp_setup"`, and a factual
`failure_reason`. Its measurement fields can all be null. It cannot contain
positive source/encoded/timing counts. These attempts remain in the evidence but
do not classify the requested capture rate as slow, unsafe or failed. They never
count as calibration or independent power repeats.

`runtime_settings` keys:

| Field | Required value |
|---|---|
| `encoder`, `encoder_preset` | Actual installed encoder and selected preset |
| `keyframe_interval_frames` | Actual positive integer setting |
| `cpu_governor`, `cpu_cap_mhz` | Actual policy and positive integer cap |
| `stock_clock` | True only after checking the actual clock configuration against its stock baseline |
| `wifi_power_policy` | Actual per-connection power policy |

Unknown runtime values can be recorded as null, but cannot qualify a preset.
`stock_clock` false or unknown cannot qualify any preset. The runner must not
default this field to true. No overclock settings are generated or enabled here.

## Measurement fields

Measurements cover the **steady-state window after warmup**, except the explicit
pre-run health mask, CPU subwindow and legacy conservative encoder maximum described below.
`warmup_s` is separate from `elapsed_s`; the 20-minute validation excludes warmup.

| Field | Meaning and unit |
|---|---|
| `window` | `"steady_state"`, `"startup"`, or null; startup cannot qualify |
| `warmup_s`, `elapsed_s` | Observed warmup and measured-window seconds |
| `source_frames` | Accepted new source frames in a closed cohort, or the legacy raw counter window delta |
| `output_frames` | Confirmed access-unit encoded frames belonging to the cohort, or the legacy encoded counter window delta; null when unknown |
| `pipeline_dropped_frames` | Measured loss counters for the defined source/pre-encoder pipeline, without duplication or double counting |
| `pipeline_drop_counts_complete` | True only when every counter needed for that pipeline loss scope is available |
| `decode_failures`, `disconnects` | Observed integer failure counts; null when not observed |
| `cpu_mean_percent` | Mean total-system CPU utilization, 0–100; aggregate the HealthMonitor deltas |
| `cpu_window` | Optional measured CPU interval bounds and coverage, described below; null when unavailable |
| `temperature_peak_c` | Maximum observed temperature in Celsius |
| `throttled_bits_before` | Raw `vcgencmd get_throttled` mask before the run |
| `throttled_bits_seen` | Bitwise OR of masks sampled throughout the window |
| `health_samples`, `health_max_gap_s` | Number of valid health samples and longest observed gap |
| `encoder_latency_ms` | Measured encoder sink-to-src timing statistic in milliseconds |
| `encoder_latency_statistic` | `"max"` or directly measured `"p95"`; a mean alone is insufficient |
| `encoder_latency_samples` | Number of matched frames underlying the timing observation |
| `queue_growing` | True/false trend established from the pre-encoder queue observations; null if unknown |
| `negotiated_wire_mode` | Actual `[width, height, refresh]` from this connection, or null |
| `whole_pi_power` | Optional separate whole-Pi input meter record below |
| `panel_latency_ms` | Null unless independently measured with physical markers |

For non-null `panel_latency_ms`, also supply
`panel_latency_method: "physical_marker_capture"` and a relative
`panel_latency_evidence_ref`. Encoder or queue timing cannot be relabeled as panel
latency. Native LCD resolution and receiver delivery/drop counts remain unknown.

### CPU sampling boundaries

`HealthMonitor.sample_timed()` supplies `cpu_interval.start_us` and `end_us`
from the actual `/proc/stat` reads, plus a separate `monotonic_us` after sensor
sampling. The accumulator uses complete, adjacent CPU intervals wholly inside
the native measurement window. Their combined duration must cover at least 80%
of that window. It computes a duration-weighted mean and retains `cpu_window`:
`start_offset_s`, `end_offset_s`, `duration_s`, `coverage`, and `intervals`.
Offsets are relative to the native window start. No warmup interval is split or
interpolated, and GUI receipt time never replaces a producer timestamp. Missing,
overlapping or incomplete CPU intervals leave the mean and subwindow unknown.

The field is optional for older externally supplied records. New accumulator
records always retain it; a numeric CPU mean from that path has a measured
subwindow. This avoids pretending asynchronous CPU and encoder samples occurred
at the exact same instant.

### Mapping current native telemetry

`panelbridge.calibration_measurement.MeasurementAccumulator` consumes direct
native events and timestamped `HealthMonitor` samples. The runner owns the
verified stream identity, shared monotonic clock, raw evidence journal, periodic
`check_deadline()` calls and hardware stop. Start health collection before the
native STREAMING transition. The caller's `streaming_us` must precede or equal
the native transition and differ by no more than the policy health-gap limit.
GUI receipt timestamps cannot substitute for producer timestamps.

The additive native `local_frame_accounting` v1 object defines a finite source
cohort. Its scope is `accepted-source-to-encoder-sink`:

- `streaming_us` anchors actual warmup. `started_us` is the first accepted source
  boundary after that warmup; `epoch` must equal it. `ended_us` closes source
  admission after the requested duration. `source_seq_first` and
  `source_seq_end_exclusive` delimit exactly the accepted population.
- `closed_us` records the later encoder-sink EOS drain. It is a separate bound,
  never added to measured duration or the thermal window. Warmup and requested
  duration must meet policy minima (5 and 30 seconds by default). Drain is
  bounded to 5 seconds; requested warmup is at most 60 seconds and duration at
  most 14,335 seconds, with a total run bound of four hours.
- `complete` requires all local boundary ledgers to settle. The accumulator also
  requires `closed`, `encoder_src_eos_seen`, available source/encoder probes and
  an internally consistent, observed cohort history. An incomplete cohort never
  supplies a complete loss count or a qualifying steady-state window.
- `accepted_source_frames` supplies `source_frames`. `emitted_wire_frames` counts
  arrivals at the encoder sink. `encoded_frames` separately supplies confirmed
  encoded output for those cohort inputs; it can remain null even when local
  pre-encoder loss is complete.
- The exact boundary keys are `appsrc`, `source_to_rate`, `rate_to_scale`, `scale`,
  `scale_to_convert`, `convert`, `convert_to_queue`, `queue`, `queue_to_encoder`.
  Their losses plus `rate_source_frames_dropped` must equal
  `pipeline_frames_dropped`. Accepted sources plus `rate_frames_duplicated` must
  equal emitted wire frames plus those losses. Rejected source admissions must
  be zero in a complete cohort.

Validation checks exact field/count shapes, finite bounds, source identity,
sequence span, conservation, immutable time boundaries, monotonic counts and
fixed population after encoder EOS. A newly reported boundary cannot precede an
earlier sample that said the boundary was still absent. Malformed, reset,
conflicting, missing or invalidated cohort evidence cannot be repaired by a later positive sample. The
cohort must first appear by its start plus the policy gap limit; periodic native
and health coverage is required throughout. These checks detect inconsistent
receipts; they do not authenticate readings or replace the raw journal.

A `final: true` event may carry the closed cohort's earlier source window when
its preceding history was observed. Its process-wide teardown counters remain
excluded. `finish()` retains the sanitized cohort, `cohort_evidence_usable`,
source `window_start_us`/`window_end_us`, and separate `cohort_drain_end_us` in
diagnostics. Persist these alongside `record`. `known_drop_deltas` stays null for
cohorts because lifetime snapshots have different boundaries; exact loss counts
are retained in `local_frame_accounting`.

Cohort encoder timing uses the measured `encoder_sink_to_src_us.max_us` and
matched population. Timing remains unknown if inputs/outputs do not all match,
an input remains pending, framing/key identity is unknown, or any invalid,
unmatched, evicted, discontinuous or ambiguous observation exists. No percentile
is inferred from mean/max. CPU intervals, temperatures and queue observations
use the source window; output drain can finish later without extending it.

**Older producers:** absent or disabled cohort accounting preserves partial
results. The first usable snapshot after warmup and last non-final snapshot
define raw/encoded counter deltas. Timing can use the conservative lifetime
maximum with the matched-count window delta. Missing pipeline boundary counters
leave `pipeline_dropped_frames` null and `pipeline_drop_counts_complete` false.

Intentional content-to-wire duplication is not a dropped frame: source cadence
is checked against `content_fps`, encoded cadence against `wire_fps`. Never infer
loss by subtracting those counters. Native telemetry leaves receiver decode
failures, disconnection coverage, queue trend and panel latency unknown.
`finish()` accepts only independent physical observations, whole-Pi meter data
and physical-marker panel latency; it has no loss/decode/queue override.

An end-point queue level alone does not prove absence of growth. The runner must
retain a bounded time series and establish the trend; queue media duration and
oldest pending age are not whole-pipeline wall-clock age.

## Selection and confidence

Default admission policy:

- At most 8 distinct candidates and 32 total records; no trial exceeds 4 hours.
- At least 5 seconds of warmup and 30 measured seconds for a provisional choice.
- Temperature below 70 °C; no active fault bits or newly observed sticky faults.
  Old sticky history is retained separately from new or current faults.
- Known CPU, thermal, health and pipeline measurements; health gaps no longer
  than 5 seconds, zero observed decode failures/disconnects and no growing queue.
- Less than 1% observed pipeline drops. Source and encoded cadence must each be
  within 98–105% of their respective requested rates.
- At least 20% encoder timing headroom using **wire refresh**, with matched timing
  coverage for at least 90% of encoded output frames and at least 30 matches.
- A preset also requires a passing 1200-second thermal window and recorded
  physical readability, geometry and motion for those exact settings/context.

These are engineering admission rules, not a hardware safety guarantee. Policy
overrides can tighten heat/headroom/drop/duration bounds but cannot relax the
documented safety and minimum-duration limits.

Recommended chooses the highest tested effective source detail, then content
rate, among candidates with duration-weighted mean CPU at or below 60% by default.
If none has moderate load, Recommended remains unavailable. Performance chooses
the highest tested detail and content rate among qualified candidates. Upscaling
to a larger wire signal does not increase effective source detail.

Battery Saver uses the lowest measured CPU load as an **estimated** choice until
every qualified candidate has at least two independent, matched-workload meter
runs using the same meter. Each power run needs at least ten samples:

```text
whole_pi_power = {
    watts_mean: measured whole-Pi input watts,
    uncertainty_watts: positive meter/measurement uncertainty,
    scope: "whole_pi_input",
    meter_id: opaque identifier,
    samples: actual integer sample count
}
```

The selector compares repeated mean input watts and retains the larger of supplied
uncertainty or half the observed run-to-run range. Overlapping intervals produce
`power: "inconclusive"`, not a unique-lowest-power claim. A disjoint minimum is
explicitly scoped to the qualified tested candidates, never all possible modes.
Receiver power cannot be included. Incomplete/unrepeated power coverage retains
the CPU estimate and never invents watts or a power-saving percentage.

## Result shape and integration

The result contains `api_version`, context/workload IDs, the actual policy,
advertised modes, validated `records`, per-trial `assessments`, `shortlist`, and
`presets`. Unknown or unsafe results preserve concrete reasons.

- `shortlist` retains provisional settings for planning the long validation;
  settings may exist while `available` is false. Do not expose these as calibrated
  Session1 presets.
- `presets` has the existing three Session1 keys with `available`, `reason`,
  `settings`, and a plain-text `measurement`. Extra fields preserve exact runtime
  settings, trial IDs, power comparison scope and granular confidence.
- `confidence` keeps physical observations, thermal validation, power, panel
  latency, receiver delivery and native resolution distinct.
- Failed measured repeats veto that candidate within the current context.
  Transport attempts and stale-context records are retained but do not poison a
  later measured candidate in the current context. Use a new context after an
  actual diagnosis/change; do not delete negative trials to obtain qualification.

The UI/controller must keep actions disabled when `available` is false. Load the
result only against a freshly verified matching context and runtime settings.

## Software evidence

UNIT-TESTED: 176 focused tests passed across measurement accumulation (80),
selection (62) and health sampling (34). Records and sensor inputs are explicitly
synthetic. Coverage includes incomplete/unsafe measurements, exact settings,
advertised modes, source-defined windows, intentional duplication and measured
drops, malformed or conflicting cohort history, EOS closure, thermal duration
excluding drain, CPU boundaries, transport aborts and power uncertainty.

OBSERVED: the accumulator parser also accepted the shape of all seven retained
native software-fixture receipts: five complete local ledgers and two explicitly
invalid ones. Their requested 0.2-second warmup and 0.6-second source window are
below calibration policy; none is a qualifying calibration trial. These checks
do not establish wireless delivery or physical panel performance.
