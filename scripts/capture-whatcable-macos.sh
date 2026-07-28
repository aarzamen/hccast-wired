#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

topology="${1:-c-to-c}"
watch_seconds="${WATCH_SECONDS:-20}"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This helper is for macOS only." >&2
  exit 2
fi

if ! command -v whatcable >/dev/null 2>&1; then
  cat >&2 <<'MSG'
The WhatCable CLI is not on PATH.
Install the free app + CLI with:
  brew install --cask darrylmorley/whatcable/whatcable
Or install CLI only with:
  brew install darrylmorley/whatcable/whatcable-cli
MSG
  exit 2
fi

system_profiler_data_types="$(system_profiler -listDataTypes 2>/dev/null || true)"
if grep -Eq '^[[:space:]]*SPUSBHostDataType[[:space:]]*$' <<< "$system_profiler_data_types"; then
  system_profiler_usb_data_type="SPUSBHostDataType"
elif grep -Eq '^[[:space:]]*SPUSBDataType[[:space:]]*$' <<< "$system_profiler_data_types"; then
  system_profiler_usb_data_type="SPUSBDataType"
else
  system_profiler_usb_data_type=""
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="logs/whatcable/${stamp}-${topology}"
mkdir -p "$out"

cat > "$out/README.txt" <<EOF2
HCCAST monitor macOS cable/port capture
UTC timestamp: ${stamp}
Topology label: ${topology}
Watch duration: ${watch_seconds} seconds

Interpretation reminder:
- WhatCable confirms cable/PD/port state exposed by macOS.
- A real direct-host success still requires a new USB VID:PID/device enumeration.
- Through a USB-A adapter, downstream USB-C CC/PD/e-marker detail is not visible.
EOF2

capture_snapshot() {
  local phase="$1"
  whatcable --json > "$out/whatcable-${phase}.json" 2> "$out/whatcable-${phase}.stderr.txt" || true
  whatcable --raw > "$out/whatcable-${phase}-raw.txt" 2>> "$out/whatcable-${phase}.stderr.txt" || true
  if [[ -n "$system_profiler_usb_data_type" ]]; then
    system_profiler "$system_profiler_usb_data_type" > "$out/system-profiler-${phase}.txt" 2>&1 || true
  else
    printf '%s\n' \
      'System Profiler USB capture unavailable: neither SPUSBHostDataType nor SPUSBDataType is advertised.' \
      > "$out/system-profiler-${phase}.txt"
  fi
  ioreg -p IOUSB -l -w0 > "$out/ioreg-${phase}.txt" 2>&1 || true
}

{
  echo "=== uname ==="
  uname -a
  echo
  echo "=== sw_vers ==="
  sw_vers
  echo
  echo "=== whatcable version ==="
  whatcable --version || true
} > "$out/environment.txt" 2>&1

echo "Disconnect the HCCAST monitor from the Mac, leave other test topology components as intended, then press Enter."
read -r
capture_snapshot before

echo "Starting WhatCable watch capture."
whatcable --watch --json > "$out/whatcable-watch.ndjson" 2> "$out/whatcable-watch.stderr.txt" &
watch_pid=$!
cleanup() {
  if kill -0 "$watch_pid" >/dev/null 2>&1; then
    kill "$watch_pid" >/dev/null 2>&1 || true
    wait "$watch_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

echo "Connect the monitor now using topology '${topology}'. Keep it powered."
echo "Watching for ${watch_seconds} seconds..."
sleep "$watch_seconds"
cleanup
trap - EXIT INT TERM

capture_snapshot after

diff -u "$out/whatcable-before-raw.txt" "$out/whatcable-after-raw.txt" > "$out/whatcable-raw.diff" || true
diff -u "$out/system-profiler-before.txt" "$out/system-profiler-after.txt" > "$out/system-profiler.diff" || true
diff -u "$out/ioreg-before.txt" "$out/ioreg-after.txt" > "$out/ioreg.diff" || true

cat <<EOF2

Capture complete:
  $out

Start review with:
  $out/whatcable-watch.ndjson
  $out/whatcable-after.json
  $out/whatcable-raw.diff
  $out/system-profiler.diff
  $out/ioreg.diff
EOF2
