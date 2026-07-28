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

for required in whatcable system_profiler ioreg log rg awk; do
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
out="$project_root/logs/whatcable/${stamp}-host-claim"
mkdir -p "$out"

run_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_query_start_local="$(date '+%Y-%m-%d %H:%M:%S')"
claim_start_utc="not-started"
claim_end_utc="not-started"
hold_seconds="${HOLD_SECONDS:-0}"
watch_pid=""
ioreg_watch_pid=""
finalized=0

: > "$out/claim-stdout.txt"
: > "$out/claim-stderr-libusb.txt"
: > "$out/ioreg-transient-target.txt"

cat > "$out/claim-command.txt" <<EOF
LIBUSB_DEBUG=4 .venv/bin/hccast-wired -vv host-claim \\
  --vendor-id 0x1cbe \\
  --product-id 0x0005 \\
  --interface 0 \\
  --wait-seconds ${WAIT_SECONDS:-120} \\
  --poll-interval ${POLL_INTERVAL:-0.01} \\
  --hold-seconds ${hold_seconds} \\
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

kernel_shows_persistent_enumeration_failure() {
  local failure_lines failure_count

  if rg -qi 'persistent enumeration failures?' "$out/kernel-log.txt"; then
    return 0
  fi

  failure_lines="$(
    rg -i \
      '(setAddress|createDevice).*(fail(ed|ure)?|error|unable|tim(e|ed)[ -]?out)|(fail(ed|ure)?|error|unable|tim(e|ed)[ -]?out).*(setAddress|createDevice)' \
      "$out/kernel-log.txt" || true
  )"
  if [[ -z "$failure_lines" ]]; then
    return 1
  fi

  failure_count="$(printf '%s\n' "$failure_lines" | wc -l | awk '{print $1}')"
  [[ "$failure_count" -ge 2 ]]
}

classify_result() {
  local status="$1"
  if [[ "$status" -eq 0 ]]; then
    printf 'CLAIM_SUCCEEDED_NO_IO\n'
  elif rg -qi 'non-detaching claim|cannot claim USB interface' \
    "$out/claim-stderr-libusb.txt"; then
    printf 'CLAIM_FAILED_NONDETACHING\n'
  elif rg -qi '1cbe:0005.*not found|USB device.*not found' \
    "$out/claim-stderr-libusb.txt"; then
    if [[ -s "$out/ioreg-transient-target.txt" ]]; then
      printf 'OTHER_FAILURE\n'
    elif kernel_shows_persistent_enumeration_failure; then
      printf 'USB_ENUMERATION_FAILED_BEFORE_TARGET_OBSERVED\n'
    else
      printf 'TARGET_NOT_OBSERVED\n'
    fi
  else
    printf 'OTHER_FAILURE\n'
  fi
}

observer_reconciliation() {
  if [[ -s "$out/ioreg-transient-target.txt" ]] \
    && rg -qi '1cbe:0005.*not found|USB device.*not found' \
      "$out/claim-stderr-libusb.txt"; then
    printf 'macOS observed the target but the claim tool did not acquire/find it\n'
  else
    printf 'no cross-observer contradiction detected\n'
  fi
}

finalize() {
  local status="$1"
  local run_end_utc log_query_end_local classification reconciliation
  if [[ "$finalized" -eq 1 ]]; then
    return 0
  fi
  finalized=1
  cleanup_watchers
  capture_snapshot after
  run_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  log_query_end_local="$(date '+%Y-%m-%d %H:%M:%S')"

  log show --start "$log_query_start_local" --end "$log_query_end_local" --style compact \
    --predicate '(process == "kernel" AND (eventMessage CONTAINS[c] "USB" OR eventMessage CONTAINS[c] "1cbe" OR eventMessage CONTAINS[c] "0005" OR eventMessage CONTAINS[c] "SCSI" OR eventMessage CONTAINS[c] "storage" OR eventMessage CONTAINS[c] "mass" OR eventMessage CONTAINS[c] "disk" OR eventMessage CONTAINS[c] "MSC")) OR subsystem CONTAINS[c] "USB"' \
    > "$out/kernel-log.txt" 2> "$out/kernel-log.stderr.txt" || true
  rg -i 'usb|iousb|xhci|1cbe|0005|interface|claim|attach|detach|reset|scsi|storage|mass|disk|msc' \
    "$out/kernel-log.txt" > "$out/usb-key-events.txt" || true

  diff -u "$out/whatcable-before-raw.txt" "$out/whatcable-after-raw.txt" \
    > "$out/whatcable-raw.diff" || true
  diff -u "$out/system-profiler-before.txt" "$out/system-profiler-after.txt" \
    > "$out/system-profiler.diff" || true
  diff -u "$out/ioreg-before.txt" "$out/ioreg-after.txt" > "$out/ioreg.diff" || true

  classification="$(classify_result "$status")"
  reconciliation="$(observer_reconciliation)"
  {
    printf 'run start UTC: %s\n' "$run_start_utc"
    printf 'claim start UTC: %s\n' "$claim_start_utc"
    printf 'claim end UTC: %s\n' "$claim_end_utc"
    printf 'run end UTC: %s\n' "$run_end_utc"
    printf 'log query start local: %s\n' "$log_query_start_local"
    printf 'log query end local: %s\n' "$log_query_end_local"
    printf 'exit status: %s\n' "$status"
    printf 'classification: %s\n' "$classification"
    printf 'observer reconciliation: %s\n' "$reconciliation"
    printf 'requested hold seconds: %s\n' "$hold_seconds"
  } > "$out/claim-result.txt"

  cat > "$out/SUMMARY.md" <<EOF
# macOS direct-host claim-only capture

- Classification: **${classification}**
- Claim command exit status: **${status}**
- Observer reconciliation: ${reconciliation}
- Requested claim hold seconds: ${hold_seconds}
- Run start UTC: ${run_start_utc}
- Run end UTC: ${run_end_utc}

Safety boundary: no HCCAST/application bulk-endpoint payload bytes were read or written;
no session was constructed. The claim-only diagnostic requested no configuration activation
and no kernel-driver detachment. USB enumeration, descriptors, and control machinery still
exist below this application boundary.

Completion of the requested hold does not establish that the device remained attached;
kernel and IORegistry evidence determine survival or detach timing.

A success proves only userspace interface ownership, not HCCAST protocol compatibility.
HCCAST remains unverified until a separately authorized exchange receives SETV.
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

Confirm those three conditions, then press Enter.
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

claim_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
set +e
LIBUSB_DEBUG=4 .venv/bin/hccast-wired -vv host-claim \
  --vendor-id 0x1cbe \
  --product-id 0x0005 \
  --interface 0 \
  --wait-seconds "${WAIT_SECONDS:-120}" \
  --poll-interval "${POLL_INTERVAL:-0.01}" \
  --hold-seconds "$hold_seconds" \
  --try-claim-with-kernel-driver \
  > "$out/claim-stdout.txt" 2> "$out/claim-stderr-libusb.txt"
claim_status=$?
set -e
claim_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

finalize "$claim_status"
trap - EXIT INT TERM
exit "$claim_status"
