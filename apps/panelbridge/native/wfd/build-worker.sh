#!/bin/bash
set -euo pipefail

# Build a private target without replacing distro GNOME Network Displays.
panelbridge_root="$(cd "$(dirname "$0")/../.." && pwd)"
archive="$panelbridge_root/vendor/gnome-network-displays-0.99.0.tar.gz"
stage="$panelbridge_root/vendor/panelbridge-wfd-graceful-build"
source_dir="$stage/gnome-network-displays-0.99.0"
build_dir="$stage/build"
expected=b6314d25be7589c621b106c1712b00277c9b4d01d4796a78e987f10c0c1d1400

actual="$(sha256sum "$archive" | cut -d ' ' -f 1)"
if [ "$actual" != "$expected" ]; then
  echo 'Pinned upstream archive hash mismatch' >&2
  exit 2
fi
mkdir -p "$stage"
if [ ! -d "$source_dir" ]; then
  tar -xzf "$archive" -C "$stage"
fi
# Refresh only our patched upstream files before applying the current small diff.
tar -xzf "$archive" -C "$stage" \
  gnome-network-displays-0.99.0/src/nd-wfd-p2p-sink.c \
  gnome-network-displays-0.99.0/src/nd-wfd-p2p-sink.h \
  gnome-network-displays-0.99.0/src/wfd/wfd-client.c \
  gnome-network-displays-0.99.0/src/wfd/wfd-media-factory.c \
  gnome-network-displays-0.99.0/src/wfd/wfd-params.c \
  gnome-network-displays-0.99.0/src/wfd/wfd-params.h \
  gnome-network-displays-0.99.0/src/wfd/meson.build
patch -d "$source_dir" -p1 < "$panelbridge_root/native/wfd/0001-adopt-managed-p2p.patch"
patch -d "$source_dir" -p1 < "$panelbridge_root/native/wfd/0002-explicit-profile.patch"
patch -d "$source_dir" -p1 < "$panelbridge_root/native/wfd/0003-runtime-metrics.patch"
patch -d "$source_dir" -p1 < "$panelbridge_root/native/wfd/0004-graceful-teardown.patch"
cp "$panelbridge_root/native/wfd/panelbridge-wfd-worker.c" "$source_dir/src/panelbridge-wfd-worker.c"
cp "$panelbridge_root/native/wfd/test-profile-gnd.c" "$source_dir/src/test-profile-gnd.c"
cp "$panelbridge_root/native/wfd/test-runtime.c" "$source_dir/src/test-runtime.c"
cp "$panelbridge_root/native/wfd/test-frame-meta.c" "$source_dir/src/test-frame-meta.c"
cp "$panelbridge_root/native/wfd/test-calibration.c" "$source_dir/src/test-calibration.c"
cp "$panelbridge_root/native/wfd/test-source-reset.c" "$source_dir/src/test-source-reset.c"
cp "$panelbridge_root/native/wfd/pb-wfd-profile.c" "$panelbridge_root/native/wfd/pb-wfd-profile.h" \
  "$panelbridge_root/native/wfd/pb-wfd-bridge.c" "$panelbridge_root/native/wfd/pb-wfd-bridge.h" \
  "$panelbridge_root/native/wfd/pb-wfd-runtime.c" "$panelbridge_root/native/wfd/pb-wfd-runtime.h" \
  "$panelbridge_root/native/wfd/pb-wfd-frame-meta.c" "$panelbridge_root/native/wfd/pb-wfd-frame-meta.h" \
  "$panelbridge_root/native/wfd/pb-wfd-telemetry.c" "$panelbridge_root/native/wfd/pb-wfd-telemetry.h" \
  "$source_dir/src/wfd/"
if ! grep -q 'PanelBridge bounded worker target' "$source_dir/src/meson.build"; then
  cat >> "$source_dir/src/meson.build" <<'MESON'

# PanelBridge bounded worker target (upstream application targets disabled).
executable('panelbridge-wfd-worker',
  ['panelbridge-wfd-worker.c'] + gnome_nd_common_sources,
  dependencies: gnome_nd_common_deps + [dependency('gstreamer-app-1.0')],
  link_with: [wfd_server, cc_cast_channel],
  install: false,
)
MESON
fi
if ! grep -q 'PanelBridge profile behavior tests' "$source_dir/src/meson.build"; then
  cat >> "$source_dir/src/meson.build" <<'MESON'

# PanelBridge profile behavior tests (synthetic advertisements, no network).
profile_test = executable('panelbridge-wfd-profile-test',
  ['test-profile-gnd.c'],
  dependencies: gnome_nd_common_deps,
  link_with: [wfd_server],
  install: false,
)
test('panelbridge-wfd-profile', profile_test)
MESON
fi
if ! grep -q 'PanelBridge runtime behavior tests' "$source_dir/src/meson.build"; then
  cat >> "$source_dir/src/meson.build" <<'MESON'

# PanelBridge runtime behavior tests (no network or GStreamer runtime).
runtime_test = executable('panelbridge-wfd-runtime-test',
  ['test-runtime.c', 'wfd/pb-wfd-runtime.c'],
  include_directories: include_directories('wfd'),
  install: false,
)
test('panelbridge-wfd-runtime', runtime_test)
MESON
fi
if ! grep -q 'PanelBridge raw source reset regression' "$source_dir/src/meson.build"; then
  cat >> "$source_dir/src/meson.build" <<'MESON'

# PanelBridge raw source reset regression (real GStreamer, no network).
source_reset_test = executable('panelbridge-wfd-source-reset-test',
  ['test-source-reset.c'] + gnome_nd_common_sources,
  dependencies: gnome_nd_common_deps + [dependency('gstreamer-app-1.0')],
  link_with: [wfd_server, cc_cast_channel],
  install: false,
)
test('panelbridge-wfd-source-reset', source_reset_test, timeout: 25)
MESON
fi
if ! grep -q 'PanelBridge calibration membership tests' "$source_dir/src/meson.build"; then
  cat >> "$source_dir/src/meson.build" <<'MESON'

# PanelBridge calibration membership tests (generated frames, no receiver).
frame_meta_test = executable('panelbridge-wfd-frame-meta-test',
  ['test-frame-meta.c', 'wfd/pb-wfd-frame-meta.c'],
  include_directories: include_directories('wfd'),
  dependencies: [dependency('gstreamer-1.0'), dependency('gstreamer-app-1.0')],
  install: false,
)
test('panelbridge-wfd-frame-meta', frame_meta_test, timeout: 30)
calibration_test = executable('panelbridge-wfd-calibration-test',
  ['test-calibration.c', 'wfd/pb-wfd-frame-meta.c', 'wfd/pb-wfd-runtime.c', 'wfd/pb-wfd-telemetry.c'],
  include_directories: include_directories('wfd'),
  dependencies: [dependency('gstreamer-1.0'), dependency('gstreamer-app-1.0'), dependency('json-glib-1.0')],
  install: false,
)
test('panelbridge-wfd-calibration', calibration_test, timeout: 30)
MESON
fi
if [ -f "$build_dir/build.ninja" ]; then
  meson setup --reconfigure "$build_dir" "$source_dir" -Dbuild_app=false -Dbuild_daemon=false -Dsystemd_resolved=false -Dfirewalld_zone=false
else
  meson setup "$build_dir" "$source_dir" -Dbuild_app=false -Dbuild_daemon=false -Dsystemd_resolved=false -Dfirewalld_zone=false
fi
meson compile -C "$build_dir" panelbridge-wfd-worker panelbridge-wfd-profile-test panelbridge-wfd-runtime-test panelbridge-wfd-source-reset-test panelbridge-wfd-frame-meta-test panelbridge-wfd-calibration-test
meson test -C "$build_dir" panelbridge-wfd-profile panelbridge-wfd-runtime panelbridge-wfd-source-reset panelbridge-wfd-frame-meta panelbridge-wfd-calibration --print-errorlogs
printf '%s\n' "$build_dir/src/panelbridge-wfd-worker"
