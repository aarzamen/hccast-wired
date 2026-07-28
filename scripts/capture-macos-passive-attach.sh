#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/.." && pwd)"
cd "$project_root"

start_mode="${1:-visible-ui}"
boot_action="not-applicable"

if [[ "$#" -gt 1 ]]; then
  echo "Expected at most one start mode: visible-ui or off-boot." >&2
  exit 2
fi

case "$start_mode" in
  visible-ui)
    default_observe_seconds="30"
    capture_suffix="passive-attach"
    ;;
  off-boot)
    default_observe_seconds="45"
    capture_suffix="passive-off-boot"
    boot_action="${BOOT_ACTION:-}"
    if [[ -z "$boot_action" ]]; then
      echo "BOOT_ACTION is required for off-boot mode." >&2
      echo "Use BOOT_ACTION=short-press or BOOT_ACTION=long-press-5s." >&2
      exit 2
    fi
    case "$boot_action" in
      short-press|long-press-5s) ;;
      *)
        echo "Invalid BOOT_ACTION: $boot_action" >&2
        echo "Use short-press or long-press-5s." >&2
        exit 2
        ;;
    esac
    ;;
  *)
    echo "Invalid start mode: $start_mode" >&2
    echo "Use visible-ui or off-boot." >&2
    exit 2
    ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This passive capture harness runs only on macOS." >&2
  exit 2
fi

for required in whatcable system_profiler ioreg log rg awk sort diff grep wc tr; do
  if ! command -v "$required" >/dev/null 2>&1; then
    echo "Required observation command is unavailable: $required" >&2
    exit 2
  fi
done

observe_seconds="${OBSERVE_SECONDS:-$default_observe_seconds}"
ioreg_poll_interval="${IOREG_POLL_INTERVAL:-0.05}"
observer_ready_attempts="${OBSERVER_READY_ATTEMPTS:-100}"
observer_ready_interval="${OBSERVER_READY_INTERVAL:-0.02}"

profiler_types="$(system_profiler -listDataTypes 2>/dev/null || true)"
if printf '%s\n' "$profiler_types" | rg -q '^[[:space:]]*SPUSBHostDataType[[:space:]]*$'; then
  profiler_usb_type="SPUSBHostDataType"
elif printf '%s\n' "$profiler_types" | rg -q '^[[:space:]]*SPUSBDataType[[:space:]]*$'; then
  profiler_usb_type="SPUSBDataType"
else
  profiler_usb_type=""
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$project_root/logs/whatcable/${stamp}-${capture_suffix}"
mkdir -p "$out"

run_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_query_start_local="not-started"
log_query_end_local="not-started"
observation_start_utc="not-started"
observation_end_utc="not-started"
whatcable_watch_pid=""
ioreg_watch_pid=""
finalized=0

: > "$out/whatcable-watch.ndjson"
: > "$out/whatcable-watch.stderr.txt"
: > "$out/ioreg-rapid-snapshots.txt"
: > "$out/ioreg-rapid-timeline.tsv"
: > "$out/new-vidpid-observations.tsv"
: > "$out/new-vidpid-unique.tsv"
: > "$out/observer-errors.txt"
: > "$out/ioreg-rapid-sample-count.txt"

extract_vidpid_records() {
  awk '
    function hex_digit(character) {
      character = tolower(character)
      if (character >= "0" && character <= "9") return character + 0
      return index("abcdef", character) + 9
    }
    function parse_number(value,    index_value, result, character) {
      gsub(/[^0-9A-Fa-fxX]/, "", value)
      if (value ~ /^0[xX]/) {
        result = 0
        for (index_value = 3; index_value <= length(value); index_value += 1) {
          character = substr(value, index_value, 1)
          result = (result * 16) + hex_digit(character)
        }
        return result
      }
      return value + 0
    }
    function emit_record() {
      if (have_vendor && have_product) {
        gsub(/[[:space:]]+/, " ", service)
        printf "%04x:%04x\t%s\n", vendor, product, service
      }
    }
    /^[[:space:]]*\+-o / {
      emit_record()
      service = $0
      vendor = product = 0
      have_vendor = have_product = 0
      next
    }
    /"idVendor"[[:space:]]*=/ {
      value = $0
      sub(/^.*"idVendor"[[:space:]]*=[[:space:]]*/, "", value)
      vendor = parse_number(value)
      have_vendor = 1
    }
    /"idProduct"[[:space:]]*=/ {
      value = $0
      sub(/^.*"idProduct"[[:space:]]*=[[:space:]]*/, "", value)
      product = parse_number(value)
      have_product = 1
    }
    END { emit_record() }
  '
}

capture_snapshot() {
  local phase="$1"
  local whatcable_json_status whatcable_raw_status ioreg_status device_status

  printf '%s\n' "$phase" > "$out/current-phase.txt"
  set +e
  HCCAST_CAPTURE_PHASE="$phase" whatcable --json > "$out/whatcable-${phase}.json" \
    2> "$out/whatcable-${phase}.stderr.txt"
  whatcable_json_status=$?
  HCCAST_CAPTURE_PHASE="$phase" whatcable --raw > "$out/whatcable-${phase}-raw.txt" \
    2>> "$out/whatcable-${phase}.stderr.txt"
  whatcable_raw_status=$?
  set -e
  if [[ "$whatcable_json_status" -ne 0 || "$whatcable_raw_status" -ne 0 ]]; then
    printf '%s WhatCable snapshot failure: json=%s raw=%s\n' \
      "$phase" "$whatcable_json_status" "$whatcable_raw_status" \
      >> "$out/observer-errors.txt"
  fi
  if [[ -n "$profiler_usb_type" ]]; then
    HCCAST_CAPTURE_PHASE="$phase" system_profiler "$profiler_usb_type" \
      > "$out/system-profiler-${phase}.txt" 2>&1 || true
  else
    printf '%s\n' \
      'USB System Profiler capture unavailable: no supported data type advertised.' \
      > "$out/system-profiler-${phase}.txt"
  fi

  set +e
  HCCAST_CAPTURE_PHASE="$phase" ioreg -p IOUSB -l -w0 \
    > "$out/ioreg-${phase}.txt" 2>&1
  ioreg_status=$?
  HCCAST_CAPTURE_PHASE="$phase" ioreg -r -c IOUSBHostDevice -l -w0 \
    > "$out/ioreg-devices-${phase}.txt" 2>&1
  device_status=$?
  set -e
  printf 'full tree status: %s\ndevice tree status: %s\n' \
    "$ioreg_status" "$device_status" > "$out/ioreg-${phase}.status.txt"
  if [[ "$ioreg_status" -ne 0 || "$device_status" -ne 0 ]]; then
    printf '%s snapshot IORegistry failure: full=%s device=%s\n' \
      "$phase" "$ioreg_status" "$device_status" >> "$out/observer-errors.txt"
  fi
  extract_vidpid_records < "$out/ioreg-devices-${phase}.txt" \
    | sort -u > "$out/${phase}-vidpid.tsv"
}

pair_in_file() {
  local pair="$1"
  local file="$2"
  awk -F '\t' -v wanted="$pair" '
    $1 == wanted { found = 1 }
    END { exit(found ? 0 : 1) }
  ' "$file"
}

rapid_ioreg_watch() {
  local sequence=0 snapshot current timestamp vidpid service status
  local current_file="$out/.ioreg-current-vidpid.tsv"
  trap 'exit 0' TERM INT

  while :; do
    sequence=$((sequence + 1))
    timestamp="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    set +e
    snapshot="$(HCCAST_CAPTURE_PHASE=watch ioreg -r -c IOUSBHostDevice -l -w0 2>&1)"
    status=$?
    set -e
    if [[ "$status" -ne 0 ]]; then
      printf '%s\t%s\tioreg status %s\n' "$timestamp" "$sequence" "$status" \
        >> "$out/observer-errors.txt"
      sleep "$ioreg_poll_interval"
      continue
    fi

    printf '%s\n' "$snapshot" | extract_vidpid_records | sort -u > "$current_file"
    {
      printf '=== snapshot %s UTC %s ===\n' "$sequence" "$timestamp"
      printf '%s\n' "$snapshot"
    } >> "$out/ioreg-rapid-snapshots.txt"

    if [[ -s "$current_file" ]]; then
      while IFS=$'\t' read -r vidpid service; do
        printf '%s\t%s\t%s\t%s\n' "$timestamp" "$sequence" "$vidpid" "$service" \
          >> "$out/ioreg-rapid-timeline.tsv"
        if ! pair_in_file "$vidpid" "$out/before-vidpid.tsv"; then
          printf '%s\t%s\t%s\t%s\n' "$timestamp" "$sequence" "$vidpid" "$service" \
            >> "$out/new-vidpid-observations.tsv"
        fi
      done < "$current_file"
    else
      printf '%s\t%s\tNONE\tno IOUSBHostDevice VID:PID records\n' \
        "$timestamp" "$sequence" >> "$out/ioreg-rapid-timeline.tsv"
    fi
    printf '%s\n' "$sequence" > "$out/ioreg-rapid-sample-count.txt"
    sleep "$ioreg_poll_interval"
  done
}

wait_for_observers() {
  local attempt sample_count
  for ((attempt = 0; attempt < observer_ready_attempts; attempt += 1)); do
    if ! kill -0 "$whatcable_watch_pid" >/dev/null 2>&1; then
      printf 'WhatCable watcher ended before observation readiness\n' \
        >> "$out/observer-errors.txt"
      return 0
    fi
    if ! kill -0 "$ioreg_watch_pid" >/dev/null 2>&1; then
      printf 'IORegistry watcher ended before observation readiness\n' \
        >> "$out/observer-errors.txt"
      return 0
    fi
    sample_count="$(cat "$out/ioreg-rapid-sample-count.txt" 2>/dev/null || printf '0')"
    if [[ "$sample_count" -gt 0 ]]; then
      return 0
    fi
    sleep "$observer_ready_interval"
  done
  printf 'Observers did not become ready within the bounded readiness wait\n' \
    >> "$out/observer-errors.txt"
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
  stop_job "$whatcable_watch_pid" "WhatCable"
  stop_job "$ioreg_watch_pid" "IORegistry"
  printf 'WhatCable watcher PID: %s\nIORegistry watcher PID: %s\nStopped UTC: %s\n' \
    "${whatcable_watch_pid:-not-started}" \
    "${ioreg_watch_pid:-not-started}" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    >> "$out/watchers-stopped.txt"
}

collect_derived_evidence() {
  local log_query_end_local="$1"
  local log_status

  set +e
  log show --start "$log_query_start_local" --end "$log_query_end_local" --style compact \
    --predicate '(process == "kernel" AND (eventMessage CONTAINS[c] "USB" OR eventMessage CONTAINS[c] "IOUSB" OR eventMessage CONTAINS[c] "1cbe" OR eventMessage CONTAINS[c] "0005" OR eventMessage CONTAINS[c] "attach" OR eventMessage CONTAINS[c] "detach" OR eventMessage CONTAINS[c] "address" OR eventMessage CONTAINS[c] "enumerat" OR eventMessage CONTAINS[c] "reset")) OR subsystem CONTAINS[c] "USB"' \
    > "$out/kernel-log.txt" 2> "$out/kernel-log.stderr.txt"
  log_status=$?
  set -e
  if [[ "$log_status" -ne 0 ]]; then
    printf 'unified log query failed with status %s\n' "$log_status" \
      >> "$out/observer-errors.txt"
  fi
  rg -i 'usb|iousb|xhci|1cbe|0005|attach|detach|address|enumerat|reset|480|high.speed' \
    "$out/kernel-log.txt" > "$out/usb-key-events.txt" || true

  diff -u "$out/whatcable-before-raw.txt" "$out/whatcable-after-raw.txt" \
    > "$out/whatcable-raw.diff" || true
  diff -u "$out/system-profiler-before.txt" "$out/system-profiler-after.txt" \
    > "$out/system-profiler.diff" || true
  diff -u "$out/ioreg-before.txt" "$out/ioreg-after.txt" > "$out/ioreg.diff" || true
  diff -u "$out/ioreg-devices-before.txt" "$out/ioreg-devices-after.txt" \
    > "$out/ioreg-devices.diff" || true
  rg '^\+[^+]' "$out/whatcable-raw.diff" \
    > "$out/whatcable-raw-added-lines.txt" || true
  rg '^\+[^+]' "$out/system-profiler.diff" \
    > "$out/system-profiler-added-lines.txt" || true

  if [[ -s "$out/new-vidpid-observations.tsv" ]]; then
    awk -F '\t' '{ print $3 "\t" $4 }' "$out/new-vidpid-observations.tsv" \
      | sort -u > "$out/new-vidpid-unique.tsv"
  else
    : > "$out/new-vidpid-unique.tsv"
  fi
}

yes_no_pattern() {
  local pattern="$1"
  shift
  if rg -qi "$pattern" "$@" 2>/dev/null; then
    printf 'yes\n'
  else
    printf 'no\n'
  fi
}

positive_attach_evidence() {
  if rg -qi \
    '("(connected|attached|connectionActive)"[[:space:]]*:[[:space:]]*true|"state"[[:space:]]*:[[:space:]]*"(attached|connected|active)"|m_connectionActive:[[:space:]]*YES|setting USB2 USB3 as DFP.*connected|USB.*attach(ed|ment)?|device.*attach(ed|ment)?|connection established|became active|found active)' \
    "$@" 2>/dev/null; then
    printf 'yes\n'
  else
    printf 'no\n'
  fi
}

finalize() {
  local original_status="$1"
  local run_end_utc classification new_count
  local target_observed target_after other_observed usb2_evidence
  local address_failure_evidence attach_evidence sample_count

  if [[ "$finalized" -eq 1 ]]; then
    return 0
  fi
  finalized=1
  if [[ -n "$whatcable_watch_pid" ]] \
    && ! kill -0 "$whatcable_watch_pid" >/dev/null 2>&1; then
    printf 'WhatCable watcher ended before cleanup\n' >> "$out/observer-errors.txt"
  fi
  if [[ -n "$ioreg_watch_pid" ]] \
    && ! kill -0 "$ioreg_watch_pid" >/dev/null 2>&1; then
    printf 'IORegistry watcher ended before cleanup\n' >> "$out/observer-errors.txt"
  fi
  cleanup_watchers
  capture_snapshot after
  run_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if [[ "$log_query_end_local" == "not-started" ]]; then
    log_query_end_local="$(date '+%Y-%m-%d %H:%M:%S')"
  fi
  collect_derived_evidence "$log_query_end_local"

  new_count="$(wc -l < "$out/new-vidpid-unique.tsv" | tr -d '[:space:]')"
  sample_count="$(cat "$out/ioreg-rapid-sample-count.txt" 2>/dev/null || printf '0')"
  target_observed="no"
  target_after="no"
  other_observed="no"
  if pair_in_file "1cbe:0005" "$out/new-vidpid-unique.tsv"; then
    target_observed="yes"
  fi
  if pair_in_file "1cbe:0005" "$out/after-vidpid.tsv"; then
    target_after="yes"
  fi
  if awk -F '\t' '$1 != "1cbe:0005" { found = 1 } END { exit(found ? 0 : 1) }' \
    "$out/new-vidpid-unique.tsv"; then
    other_observed="yes"
  fi

  usb2_evidence="$(yes_no_pattern \
    '(480[[:space:]]*(Mb/s|Mbps)|TransportsActive.*USB2|USB2.*active:[[:space:]]*(YES|true)|Only USB 2[.]0 is active|USB 2[.]0 only|USB2 device attached at high[-[:space:]]*speed)' \
    "$out/whatcable-raw-added-lines.txt" \
    "$out/system-profiler-added-lines.txt" "$out/kernel-log.txt")"
  address_failure_evidence="$(yes_no_pattern \
    '(setAddress|device address|createDevice|enumerat).*(fail|error|unable|timed?[ -]?out)|(fail|error|unable|timed?[ -]?out).*(setAddress|device address|createDevice|enumerat)' \
    "$out/kernel-log.txt")"
  attach_evidence="$(positive_attach_evidence \
    "$out/whatcable-raw-added-lines.txt" \
    "$out/system-profiler-added-lines.txt" "$out/kernel-log.txt")"
  if [[ "$new_count" -gt 0 ]]; then
    attach_evidence="yes"
  fi

  if [[ "$original_status" -ne 0 ]]; then
    classification="RUN_ABORTED"
  elif [[ -s "$out/observer-errors.txt" || "$sample_count" -eq 0 ]]; then
    classification="OBSERVATION_INCOMPLETE"
  elif [[ "$target_observed" == "yes" && "$target_after" == "yes" ]]; then
    classification="TARGET_1CBE_0005_PRESENT_AFTER_WINDOW"
  elif [[ "$target_observed" == "yes" ]]; then
    classification="TARGET_1CBE_0005_TRANSIENT"
  elif [[ "$other_observed" == "yes" ]]; then
    classification="OTHER_VID_PID_OBSERVED"
  elif [[ "$usb2_evidence" == "yes" && "$address_failure_evidence" == "yes" ]]; then
    classification="USB2_ATTACH_ADDRESS_STAGE_ENUMERATION_FAILURE"
  elif [[ "$attach_evidence" == "yes" ]]; then
    classification="USB_ATTACH_WITHOUT_DEVICE_ENUMERATION"
  else
    classification="NO_USB_ATTACH_OBSERVED"
  fi

  {
    printf 'run start UTC: %s\n' "$run_start_utc"
    printf 'observation start UTC: %s\n' "$observation_start_utc"
    printf 'observation end UTC: %s\n' "$observation_end_utc"
    printf 'run end UTC: %s\n' "$run_end_utc"
    printf 'log query start local: %s\n' "$log_query_start_local"
    printf 'log query end local: %s\n' "$log_query_end_local"
    printf 'original exit status: %s\n' "$original_status"
    printf 'classification: %s\n' "$classification"
    printf 'observation seconds: %s\n' "$observe_seconds"
    printf 'start mode: %s\n' "$start_mode"
    printf 'boot action: %s\n' "$boot_action"
    printf 'rapid IORegistry samples: %s\n' "$sample_count"
    printf 'new VID:PID count: %s\n' "$new_count"
    printf '1cbe:0005 observed: %s\n' "$target_observed"
    printf '1cbe:0005 present after observation window: %s\n' "$target_after"
    printf 'other VID:PID observed: %s\n' "$other_observed"
    printf 'usb2 attach evidence: %s\n' "$usb2_evidence"
    printf 'address-stage failure evidence: %s\n' "$address_failure_evidence"
    printf 'general attach evidence: %s\n' "$attach_evidence"
  } > "$out/capture-result.txt"

  cat > "$out/SUMMARY.md" <<EOF
# macOS passive forced-host attach capture

- Classification: **${classification}**
- Observation window: **${observe_seconds} seconds**
- Start mode: **${start_mode}**
- Boot action: **${boot_action}**
- Rapid IORegistry samples: **${sample_count}**
- New VID:PID count: **${new_count}**
- Observed 1cbe:0005: **${target_observed}**
- 1cbe:0005 present after window: **${target_after}**
- USB 2 attach evidence: **${usb2_evidence}**
- Address-stage enumeration-failure evidence: **${address_failure_evidence}**
- Run start UTC: ${run_start_utc}
- Run end UTC: ${run_end_utc}

This is an observation-only run: no PyUSB/libusb, no interface claim, no endpoint read/write,
no SETR or other HCCAST payload, no configuration activation, no driver detachment, and
no application-requested USB reset. The harness only queried macOS/WhatCable state and logs.

\`TARGET_1CBE_0005_PRESENT_AFTER_WINDOW\` means the target was observed during the run and in
the after snapshot. It does not prove continuous presence across every rapid sample.

If an identity other than 1cbe:0005 appears, preserve it exactly and do not assume this is HCCAST
until its descriptors and behavior are separately validated. A no-attach classification means only
that these observers saw no attach during this bounded run; it is not proof that the cable or screen
can never establish a link.
EOF

  printf '\nPassive capture complete.\nEvidence: %s\nClassification: %s\n' \
    "$out" "$classification"
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
  printf 'observation seconds: %s\n' "$observe_seconds"
  printf 'start mode: %s\n' "$start_mode"
  printf 'boot action: %s\n' "$boot_action"
  printf 'IORegistry poll interval: %s\n' "$ioreg_poll_interval"
  printf '\n=== uname ===\n'
  uname -a
  printf '\n=== sw_vers ===\n'
  sw_vers
  printf '\n=== WhatCable ===\n'
  whatcable --version
  printf '\n=== USB System Profiler type ===\n%s\n' "${profiler_usb_type:-unavailable}"
} > "$out/environment.txt" 2>&1

if [[ "$start_mode" == "off-boot" ]]; then
  precondition_lines="- Stage 1 power-button characterization is complete.
- The screen is deliberately powered off, not an automatic timeout state.
- External POWER is connected and stable.
- DATA is disconnected from the Mac and screen.
- The Mac charger and adapter-chain topology are stationary.
- Recorded boot action: ${boot_action}."
else
  precondition_lines="- The screen is externally powered through its POWER port.
- The screen is visibly stable on the QR/setup UI.
- DATA is disconnected from the Mac and screen."
fi
cat <<PRECONDITION
Precondition before arming:
${precondition_lines}

Confirm those conditions, then press Enter.
PRECONDITION
read -r

capture_snapshot before

log_query_start_local="$(date '+%Y-%m-%d %H:%M:%S')"

whatcable --watch --json > "$out/whatcable-watch.ndjson" \
  2> "$out/whatcable-watch.stderr.txt" &
whatcable_watch_pid=$!
rapid_ioreg_watch &
ioreg_watch_pid=$!
wait_for_observers

if [[ "$start_mode" == "off-boot" ]]; then
  cat <<ARMED
ARMED: Connect DATA exactly once, then perform the recorded boot action exactly once: ${boot_action}.
Do not press the power button again, reconnect cables, or otherwise change the topology during
the ${observe_seconds}-second observation window. This harness is passive and will not claim
the interface or send USB payloads.
ARMED
else
  cat <<ARMED
ARMED: Connect DATA exactly once. Do not power-cycle, press buttons, reconnect,
or otherwise change the topology during the ${observe_seconds}-second observation window.
This harness is passive and will not claim the interface or send USB payloads.
ARMED
fi

observation_start_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'watch\n' > "$out/current-phase.txt"
sleep "$observe_seconds"
observation_end_utc="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
log_query_end_local="$(date '+%Y-%m-%d %H:%M:%S')"

finalize 0
trap - EXIT INT TERM
exit 0
