/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#define PB_TIMING_CAPACITY 64
#define PB_TIMING_NO_KEY UINT64_MAX

typedef struct {
  uint64_t key, entered_us;
  bool used;
} PbTimingEntry;
typedef struct {
  PbTimingEntry pending[PB_TIMING_CAPACITY];
  size_t next;
  uint64_t entered, exited, matched, unmatched, evicted, invalid, discontinuities;
  uint64_t last_us, max_us;
  double mean_us;
  bool ambiguous;
} PbTiming;

const char *pb_runtime_rejection(bool continuous, bool seconds_explicit,
    bool adopted, bool raw, bool list, bool self_test);
unsigned pb_runtime_seconds(bool continuous, unsigned seconds);
const char *pb_calibration_rejection(bool warmup_set, bool duration_set,
    int warmup_ms, int duration_ms, bool raw, bool adopted, bool continuous,
    bool seconds_explicit, bool list, bool self_test);
void pb_timing_enter(PbTiming *timing, uint64_t key, uint64_t now_us);
void pb_timing_exit(PbTiming *timing, uint64_t key, uint64_t now_us);
void pb_timing_discontinuity(PbTiming *timing);
bool pb_timing_oldest_age(const PbTiming *timing, uint64_t now_us, uint64_t *age_us);

/* Bounded FIFO membership accounting. Locks are owned by the caller. A source
 * identity uses pts=0; after videorate it uses the actual output PTS. Counts
 * become exact only after EOS closure. Buffer addresses/OFFSET are not keys. */
#define PB_LOSS_CAPACITY 64
typedef struct { uint64_t sequence, pts; } PbFrameKey;
typedef struct {
  PbFrameKey key;
  unsigned result; /* 0: push result pending, 1: accepted, 2: rejected */
  unsigned disposition; /* 0: pending, 1: emitted, 2: dropped */
} PbLossEntry;
typedef struct {
  PbLossEntry pending[PB_LOSS_CAPACITY];
  size_t length;
  PbFrameKey last_enter, last_exit;
  bool has_enter, has_exit, closed;
  const char *invalid_reason;
  uint64_t attempts, accepted, rejected, emitted, dropped;
} PbLoss;
void pb_loss_enter(PbLoss *loss, PbFrameKey key, bool awaiting_push_result);
void pb_loss_push_result(PbLoss *loss, PbFrameKey key, bool accepted);
void pb_loss_exit(PbLoss *loss, PbFrameKey key);
void pb_loss_close(PbLoss *loss);
bool pb_loss_complete(const PbLoss *loss);
void pb_loss_invalidate(PbLoss *loss, const char *reason);

typedef struct { uint64_t sequence, outputs; } PbRateEntry;
typedef struct {
  PbRateEntry pending[PB_LOSS_CAPACITY];
  size_t length;
  PbFrameKey last_exit;
  uint64_t last_enter, entered, emitted, dropped, duplicated;
  bool has_enter, has_exit, closed;
  const char *invalid_reason;
} PbRateLoss;
void pb_rate_loss_enter(PbRateLoss *loss, uint64_t sequence);
void pb_rate_loss_exit(PbRateLoss *loss, PbFrameKey key);
void pb_rate_loss_close(PbRateLoss *loss);
bool pb_rate_loss_complete(const PbRateLoss *loss);

typedef struct {
  uint64_t warmup_us, duration_us, streaming_us, started_us, ended_us;
  bool streaming, started, ended;
} PbCohortClock;
void pb_cohort_streaming(PbCohortClock *clock, uint64_t now_us);
/* 0: warmup/not streaming; 1: belongs to cohort; -1: EOS drain required. */
int pb_cohort_admit(PbCohortClock *clock, uint64_t now_us);
