#!/usr/bin/env bash
set -euo pipefail

output="${1:-test-pattern-1280x720.h264}"
duration="${DURATION:-15}"
fps="${FPS:-30}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo 'ffmpeg is required.' >&2
  exit 1
fi

# AUD NALs give the Python streamer unambiguous access-unit boundaries.  Repeat
# SPS/PPS at every IDR so a fixed-function decoder can recover after a drop.
ffmpeg -hide_banner -y \
  -f lavfi -i "testsrc2=size=1280x720:rate=${fps}" \
  -f lavfi -i "sine=frequency=1000:sample_rate=48000" \
  -t "$duration" \
  -map 0:v:0 \
  -an \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -profile:v baseline -level 3.1 -pix_fmt yuv420p \
  -b:v 6000k -maxrate 8000k -bufsize 8000k \
  -g "$fps" -keyint_min "$fps" -sc_threshold 0 \
  -x264-params "aud=1:repeat-headers=1" \
  -f h264 "$output"

echo "Created $output"
