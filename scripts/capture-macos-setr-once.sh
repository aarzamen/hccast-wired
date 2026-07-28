#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
cd "$project_root"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This capture harness runs only on macOS." >&2
  exit 2
fi

driver="$project_root/.venv/bin/hccast-wired"
if [[ ! -x "$driver" ]]; then
  echo "Missing executable: $driver" >&2
  exit 2
fi

for required in whatcable system_profiler ioreg log rg awk sed xxd; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Required command is unavailable: $required" >&2
    exit 2
  fi
done

profiler_types="$(system_profiler -listDataTypes 2>/dev/null || true)"
if printf '%s\n' "$profiler_types" | rg -q '^[[:space:]]*SPUSBHostDataType[[:space:]]*$'; then
  profiler_usb_type="SPUSBHostDataType"
elif printf '%s\n' "$profiler_types" | rg -q '^[[:space:]]*SPUSBDataType[[:space:]]*$'; then
  profiler_usb_type="SPUSBDataType"
else
  profiler_usb_type=""
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$project_root/logs/whatcable/${stamp}-host-setr-once"
mkdir -p "$out"

run_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_query_start_local="$(date '+%Y-%m-%d %H:%M:%S')"
probe_start_utc="not-started"
probe_end_utc="not-started"
watch_pid=""
ioreg_watch_pid=""
finalized=0
final_exit_status=0
response_timeout_ms=500
outbound_hex="00 00 00 14 00 00 00 00 52 54 45 53 00 00 00 01 00 00 00 00"
raw_output="$out/raw-response.bin"

: > "$out/probe-stdout.json"
: > "$out/probe-stderr-libusb.txt"
: > "$out/ioreg-transient-target.txt"
: > "$raw_output"
printf '%s\n' "$outbound_hex" > "$out/setr-outbound.hex"

cat > "$out/probe-command.txt" <<EOF
LIBUSB_DEBUG=4 .venv/bin/hccast-wired -vv host-setr-once \\
  --vendor-id 0x1cbe \\
  --product-id 0x0005 \\
  --interface 0 \\
  --wait-seconds ${WAIT_SECONDS:-120} \\
  --poll-interval ${POLL_INTERVAL:-0.01} \\
  --response-timeout-ms 500 \\
  --raw-output ${raw_output} \\
  --try-claim-with-kernel-driver
EOF

capture_snapshot() {
  local phase="$1"
  whatcable --json > "$out/whatcable-${phase}.json" \
    2> "$out/whatcable-${phase}.stderr.txt" || true
  whatcable --raw > "$out/whatcable-${phase}-raw.txt" \
    2>> "$out/whatcable-${phase}.stderr.txt" || true
  if [[ -n "$profiler_usb_type" ]]; then
    system_profiler "$profiler_usb_type" > "$out/system-profiler-${phase}.txt" 2>&1 || true
  else
    printf '%s\n' \
      'USB System Profiler capture unavailable: no supported data type advertised.' \
      > "$out/system-profiler-${phase}.txt"
  fi
  ioreg -p IOUSB -l -w0 > "$out/ioreg-${phase}.txt" 2>&1 || true
}

extract_target_ioreg_record() {
  awk '
    function target_record(text) {
      return text ~ /"idVendor"[[:space:]]*=[[:space:]]*7358([^0-9]|$)/ && \
             text ~ /"idProduct"[[:space:]]*=[[:space:]]*5([^0-9]|$)/
    }
    function emit_if_target() {
      if (length(record) > 0 && target_record(record)) {
        printf "%s", record
        found = 1
      }
    }
    /^\+-o / {
      emit_if_target()
      if (found) {
        exit
      }
      record = $0 ORS
      next
    }
    {
      record = record $0 ORS
    }
    END {
      if (!found) {
        emit_if_target()
      }
    }
  '
}

rapid_ioreg_watch() {
  local snapshot target_record
  while :; do
    snapshot="$(ioreg -r -c IOUSBHostInterface -l -w0 2>&1 || true)"
    target_record="$(printf '%s\n' "$snapshot" | extract_target_ioreg_record)"
    if [[ -n "$target_record" ]]; then
      {
        printf 'Observed UTC: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        printf '%s\n' "$target_record"
      } > "$out/ioreg-transient-target.txt"
      return 0
    fi
    sleep "${IOREG_POLL_INTERVAL:-0.02}"
  done
}

stop_job() {
  local pid="$1"
  local label="$2"
  local attempts="${WATCHER_STOP_ATTEMPTS:-25}"
  local interval="${WATCHER_STOP_INTERVAL:-0.02}"
  local attempt

  if [[ -z "$pid" ]]; then
    printf '%s watcher: not started\n' "$label" >> "$out/watchers-stopped.txt"
    return 0
  fi

  if ! kill -0 "$pid" >/dev/null 2>&1; then
    wait "$pid" 2>/dev/null || true
    printf '%s watcher PID %s: already stopped\n' \
      "$label" "$pid" >> "$out/watchers-stopped.txt"
    return 0
  fi

  kill -TERM "$pid" >/dev/null 2>&1 || true
  for ((attempt = 0; attempt < attempts; attempt += 1)); do
    if ! kill -0 "$pid" >/dev/null 2>&1; then
      wait "$pid" 2>/dev/null || true
      printf '%s watcher PID %s: stopped after TERM\n' \
        "$label" "$pid" >> "$out/watchers-stopped.txt"
      return 0
    fi
    sleep "$interval"
  done

  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -KILL "$pid" >/dev/null 2>&1 || true
    printf '%s watcher PID %s: forced KILL after bounded TERM wait\n' \
      "$label" "$pid" >> "$out/watchers-stopped.txt"
  fi
  wait "$pid" 2>/dev/null || true
}

cleanup_watchers() {
  : > "$out/watchers-stopped.txt"
  stop_job "$watch_pid" "WhatCable"
  stop_job "$ioreg_watch_pid" "IORegistry"
  printf 'WhatCable watcher PID: %s\nIORegistry watcher PID: %s\nStopped UTC: %s\n' \
    "${watch_pid:-not-started}" \
    "${ioreg_watch_pid:-not-started}" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >> "$out/watchers-stopped.txt"
}

classify_result() {
  local status="$1"
  local reported
  reported="$(
    sed -nE \
      's/.*"classification"[[:space:]]*:[[:space:]]*"([A-Z_]+)".*/\1/p' \
      "$out/probe-stdout.json" | sed -n '1p'
  )"
  if [[ "$status" -eq 0 ]]; then
    case "$reported" in
      VALID_SETV|PARTIAL_HCCAST_RESPONSE|RAW_NON_HCCAST_RESPONSE|SETR_WRITE_OK_NO_RESPONSE|SETR_WRITE_FAILED)
        printf '%s\n' "$reported"
        return 0
        ;;
    esac
  elif [[ "$status" -eq 3 && "$reported" == "SETR_WRITE_FAILED" ]]; then
    printf 'SETR_WRITE_FAILED\n'
    return 0
  fi
  printf 'COMMAND_ERROR\n'
}

classification_note() {
  local classification="$1"
  case "$classification" in
    VALID_SETV)
      printf '%s\n' \
        'The dedicated probe reported one complete, structurally valid SETV frame.'
      ;;
    PARTIAL_HCCAST_RESPONSE)
      printf '%s\n' \
        'Response bytes resembled an incomplete HCCAST frame; this is suggestive but inconclusive.'
      ;;
    RAW_NON_HCCAST_RESPONSE)
      printf '%s\n' \
        'Response bytes arrived but did not contain a structurally valid SETV frame.'
      ;;
    SETR_WRITE_OK_NO_RESPONSE)
      printf '%s\n' \
        'The one SETR write completed but no response arrived; this does not disprove HCCAST.'
      ;;
    SETR_WRITE_FAILED)
      printf '%s\n' \
        'The one outbound SETR write did not complete.'
      ;;
    *)
      printf '%s\n' \
        'The command failed or its result could not be classified; inspect stdout and stderr.'
      ;;
  esac
}

finalize() {
  local status="$1"
  local run_end_utc log_query_end_local classification note response_bytes
  local evidence_status xxd_status
  if [[ "$finalized" -eq 1 ]]; then
    return 0
  fi
  finalized=1
  final_exit_status="$status"
  cleanup_watchers
  capture_snapshot after
  run_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log_query_end_local="$(date '+%Y-%m-%d %H:%M:%S')"

  log show --start "$log_query_start_local" --end "$log_query_end_local" --style compact \
    --predicate '(process == "kernel" AND (eventMessage CONTAINS[c] "USB" OR eventMessage CONTAINS[c] "1cbe" OR eventMessage CONTAINS[c] "0005" OR eventMessage CONTAINS[c] "interface" OR eventMessage CONTAINS[c] "claim" OR eventMessage CONTAINS[c] "attach" OR eventMessage CONTAINS[c] "detach" OR eventMessage CONTAINS[c] "reset")) OR subsystem CONTAINS[c] "USB"' \
    > "$out/kernel-log.txt" 2> "$out/kernel-log.stderr.txt" || true
  rg -i 'usb|iousb|xhci|1cbe|0005|interface|claim|attach|detach|reset' \
    "$out/kernel-log.txt" > "$out/usb-key-events.txt" || true

  diff -u "$out/whatcable-before-raw.txt" "$out/whatcable-after-raw.txt" \
    > "$out/whatcable-raw.diff" || true
  diff -u "$out/system-profiler-before.txt" "$out/system-profiler-after.txt" \
    > "$out/system-profiler.diff" || true
  diff -u "$out/ioreg-before.txt" "$out/ioreg-after.txt" > "$out/ioreg.diff" || true

  response_bytes="$(wc -c < "$raw_output" | tr -d '[:space:]')"
  evidence_status="OK"
  xxd_status=0
  : > "$out/raw-response-xxd.stderr.txt"
  if [[ "$response_bytes" -eq 0 ]]; then
    : > "$out/raw-response.hex"
  else
    if xxd -g 1 -c 16 "$raw_output" \
      > "$out/raw-response.hex" 2> "$out/raw-response-xxd.stderr.txt"; then
      xxd_status=0
    else
      xxd_status=$?
    fi
    if [[ "$xxd_status" -ne 0 || ! -s "$out/raw-response.hex" ]]; then
      evidence_status="RAW_HEX_RENDER_FAILED"
      if [[ "$xxd_status" -eq 0 ]]; then
        printf '%s\n' \
          'xxd returned success but produced no hex for a nonempty raw response.' \
          >> "$out/raw-response-xxd.stderr.txt"
      fi
      if [[ "$final_exit_status" -eq 0 ]]; then
        final_exit_status=74
      fi
    fi
  fi
  classification="$(classify_result "$status")"
  note="$(classification_note "$classification")"

  {
    printf 'run start UTC: %s\n' "$run_start_utc"
    printf 'probe start UTC: %s\n' "$probe_start_utc"
    printf 'probe end UTC: %s\n' "$probe_end_utc"
    printf 'run end UTC: %s\n' "$run_end_utc"
    printf 'log query start local: %s\n' "$log_query_start_local"
    printf 'log query end local: %s\n' "$log_query_end_local"
    printf 'exit status: %s\n' "$status"
    printf 'classification: %s\n' "$classification"
    printf 'evidence status: %s\n' "$evidence_status"
    printf 'raw hex renderer exit status: %s\n' "$xxd_status"
    printf 'response timeout milliseconds: %s\n' "$response_timeout_ms"
    printf 'raw response bytes: %s\n' "$response_bytes"
    printf 'exact outbound hex: %s\n' "$outbound_hex"
    printf 'interpretation: %s\n' "$note"
  } > "$out/probe-result.txt"

  cat > "$out/SUMMARY.md" <<EOF
# macOS one-shot SETR identity capture

- Classification: **${classification}**
- Probe command exit status: **${status}**
- Evidence status: **${evidence_status}**
- Raw response bytes preserved: **${response_bytes}**
- Response window: **${response_timeout_ms} ms**
- Exact outbound bytes: ${outbound_hex}
- Run start UTC: ${run_start_utc}
- Run end UTC: ${run_end_utc}

${note}

Safety boundary: this dedicated command attempts exactly one SETR and never retries.
It requests no application configuration activation and no kernel-driver detachment.
It sends no settings, screen-information, audio, video, keepalive, stop, upgrade,
storage, or streaming command and performs no network or vendor-cloud operation.

A valid SETV would prove compatibility with this narrow HCCAST identity exchange;
it does not establish general display compatibility or a working wired video path.
Any non-response remains inconclusive because timing or link stability may be responsible.
EOF

  printf '\nCapture complete.\nEvidence: %s\nClassification: %s\n' "$out" "$classification"
}

on_exit() {
  local status=$?
  finalize "$status" || true
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

{
  printf 'project root: %s\n' "$project_root"
  printf 'run start UTC: %s\n' "$run_start_utc"
  printf '\n=== uname ===\n'
  uname -a
  printf '\n=== sw_vers ===\n'
  sw_vers
  printf '\n=== WhatCable ===\n'
  whatcable --version
  printf '\n=== USB System Profiler type ===\n%s\n' "${profiler_usb_type:-unavailable}"
} > "$out/environment.txt" 2>&1

cat <<'PRECONDITION'
Precondition before arming:
- The screen is adequately charged and externally powered through its POWER port.
- The screen is fully OFF.
- The DATA connection is disconnected from the Mac.

Confirm those conditions, then press Enter.
PRECONDITION
read -r

capture_snapshot before

whatcable --watch --json > "$out/whatcable-watch.ndjson" \
  2> "$out/whatcable-watch.stderr.txt" &
watch_pid=$!
rapid_ioreg_watch &
ioreg_watch_pid=$!

cat <<'ARMED'
ARMED: Connect DATA to the screen while leaving POWER connected. Make one boot
attempt using this unit's normal 5+ second power-button hold. Do not cycle it
again while this command is waiting.
ARMED

probe_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
LIBUSB_DEBUG=4 .venv/bin/hccast-wired -vv host-setr-once \
  --vendor-id 0x1cbe \
  --product-id 0x0005 \
  --interface 0 \
  --wait-seconds "${WAIT_SECONDS:-120}" \
  --poll-interval "${POLL_INTERVAL:-0.01}" \
  --response-timeout-ms 500 \
  --raw-output "$raw_output" \
  --try-claim-with-kernel-driver \
  > "$out/probe-stdout.json" 2> "$out/probe-stderr-libusb.txt"
probe_status=$?
set -e
probe_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

finalize "$probe_status"
run_status="$final_exit_status"
trap - EXIT INT TERM
exit "$run_status"
