/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "pb-wfd-runtime.h"
#include <string.h>

const char *pb_runtime_rejection(bool continuous, bool seconds_explicit,
    bool adopted, bool raw, bool list, bool self_test) {
  if (!continuous) return NULL;
  if (seconds_explicit) return "continuous-and-seconds-are-exclusive";
  if (!adopted || !raw || list || self_test) return "continuous-requires-adopted-raw-session";
  return NULL;
}

unsigned pb_runtime_seconds(bool continuous, unsigned seconds) {
  return continuous ? 0 : seconds;
}

const char *pb_calibration_rejection(bool warmup_set,bool duration_set,
    int warmup_ms,int duration_ms,bool raw,bool adopted,bool continuous,
    bool seconds_explicit,bool list,bool self_test) {
  if(!warmup_set && !duration_set)return NULL;
  if(!warmup_set || !duration_set)return "calibration-requires-warmup-and-duration";
  if(!raw || !adopted || list || self_test)return "calibration-requires-adopted-raw-session";
  if(continuous || seconds_explicit)return "calibration-has-its-own-bounded-duration";
  if(warmup_ms<5000 || warmup_ms>60000 || duration_ms<30000 ||
      duration_ms>14335000 || warmup_ms+duration_ms>14395000)
    return "calibration-time-bounds";
  return NULL;
}

void pb_timing_enter(PbTiming *t, uint64_t key, uint64_t now_us) {
  t->entered++;
  if (key == PB_TIMING_NO_KEY || t->ambiguous) { t->invalid++; return; }
  for (size_t i=0; i<PB_TIMING_CAPACITY; i++) {
    if (t->pending[i].used && t->pending[i].key==key) {
      /* Never guess which duplicate timestamp an output belongs to. */
      t->ambiguous=true;
      t->invalid++;
      return;
    }
  }
  size_t slot=t->next;
  for (size_t i=0; i<PB_TIMING_CAPACITY; i++) {
    size_t candidate=(t->next+i)%PB_TIMING_CAPACITY;
    if (!t->pending[candidate].used) { slot=candidate; break; }
  }
  if (t->pending[slot].used) t->evicted++;
  t->pending[slot]=(PbTimingEntry){key,now_us,true};
  t->next=(slot+1)%PB_TIMING_CAPACITY;
}

void pb_timing_exit(PbTiming *t, uint64_t key, uint64_t now_us) {
  t->exited++;
  if (key == PB_TIMING_NO_KEY || t->ambiguous) { t->unmatched++; return; }
  for (size_t i=0; i<PB_TIMING_CAPACITY; i++) {
    PbTimingEntry *entry=&t->pending[i];
    if (!entry->used || entry->key!=key) continue;
    entry->used=false;
    if (now_us < entry->entered_us) { t->invalid++; return; }
    uint64_t elapsed=now_us-entry->entered_us;
    t->matched++;
    t->last_us=elapsed;
    if (elapsed>t->max_us) t->max_us=elapsed;
    t->mean_us+=((double)elapsed-t->mean_us)/(double)t->matched;
    return;
  }
  t->unmatched++;
}

void pb_timing_discontinuity(PbTiming *t) {
  memset(t->pending,0,sizeof(t->pending));
  t->next=0;
  t->ambiguous=false;
  t->discontinuities++;
}

bool pb_timing_oldest_age(const PbTiming *t, uint64_t now_us, uint64_t *age_us) {
  if (t->ambiguous) return false;
  bool found=false;
  uint64_t oldest=UINT64_MAX;
  for (size_t i=0; i<PB_TIMING_CAPACITY; i++) {
    const PbTimingEntry *entry=&t->pending[i];
    if (entry->used && entry->entered_us<=now_us && entry->entered_us<oldest) {
      oldest=entry->entered_us; found=true;
    }
  }
  if (found) *age_us=now_us-oldest;
  return found;
}

static bool key_equal(PbFrameKey a,PbFrameKey b) {
  return a.sequence==b.sequence && a.pts==b.pts;
}
static bool key_after(PbFrameKey a,PbFrameKey b) {
  return a.sequence>b.sequence || (a.sequence==b.sequence && a.pts>b.pts);
}
static bool key_valid(PbFrameKey key) {
  return key.sequence!=UINT64_MAX && key.pts!=UINT64_MAX;
}
void pb_loss_invalidate(PbLoss *l,const char *reason) {
  if(!l->invalid_reason)l->invalid_reason=reason;
}
static void retire(PbLoss *l) {
  size_t count=0;
  while(count<l->length) {
    PbLossEntry *e=&l->pending[count];
    if(!e->result || (!e->disposition && e->result!=2))break;
    if(e->result==1 && e->disposition==2)l->dropped++;
    count++;
  }
  if(count) {
    l->length-=count;
    memmove(l->pending,l->pending+count,l->length*sizeof *l->pending);
  }
}
void pb_loss_enter(PbLoss *l,PbFrameKey key,bool awaiting_push_result) {
  l->attempts++;
  if(!key_valid(key)) { pb_loss_invalidate(l,"missing-frame-key");return; }
  if(l->closed) { pb_loss_invalidate(l,"input-after-eos");return; }
  if(l->has_enter && !key_after(key,l->last_enter)) {
    pb_loss_invalidate(l,"nonmonotonic-input-key");return;
  }
  l->has_enter=true;l->last_enter=key;
  if(l->length==PB_LOSS_CAPACITY) { pb_loss_invalidate(l,"tracking-overflow");return; }
  l->pending[l->length++]=(PbLossEntry){key,awaiting_push_result?0:1,0};
  if(!awaiting_push_result)l->accepted++;
}
void pb_loss_push_result(PbLoss *l,PbFrameKey key,bool accepted) {
  for(size_t i=0;i<l->length;i++) {
    PbLossEntry *e=&l->pending[i];
    if(!key_equal(e->key,key))continue;
    if(e->result) { pb_loss_invalidate(l,"duplicate-push-result");return; }
    e->result=accepted?1:2;
    if(accepted)l->accepted++;else l->rejected++;
    if(!accepted && e->disposition==1)pb_loss_invalidate(l,"rejected-push-observed");
    retire(l);return;
  }
  pb_loss_invalidate(l,"push-result-without-attempt");
}
void pb_loss_exit(PbLoss *l,PbFrameKey key) {
  if(!key_valid(key)) { pb_loss_invalidate(l,"missing-frame-key");return; }
  if(l->closed) { pb_loss_invalidate(l,"output-after-eos");return; }
  if(l->has_exit && !key_after(key,l->last_exit)) {
    pb_loss_invalidate(l,"nonmonotonic-output-key");return;
  }
  l->has_exit=true;l->last_exit=key;
  for(size_t i=0;i<l->length;i++) {
    PbLossEntry *e=&l->pending[i];
    if(!key_equal(e->key,key))continue;
    if(e->disposition) { pb_loss_invalidate(l,"repeated-output-key");return; }
    for(size_t j=0;j<i;j++)if(!l->pending[j].disposition)l->pending[j].disposition=2;
    e->disposition=1;l->emitted++;
    if(e->result==2)pb_loss_invalidate(l,"rejected-push-observed");
    retire(l);return;
  }
  pb_loss_invalidate(l,"output-without-input");
}
void pb_loss_close(PbLoss *l) {
  l->closed=true;
  for(size_t i=0;i<l->length;i++)if(!l->pending[i].disposition)l->pending[i].disposition=2;
  retire(l);
}
bool pb_loss_complete(const PbLoss *l) {
  return l->closed && !l->invalid_reason && l->length==0 &&
      l->attempts==l->accepted+l->rejected && l->accepted==l->emitted+l->dropped;
}
static void rate_invalid(PbRateLoss *l,const char *reason) {
  if(!l->invalid_reason)l->invalid_reason=reason;
}
static void rate_retire(PbRateLoss *l,size_t count) {
  for(size_t i=0;i<count;i++)if(!l->pending[i].outputs)l->dropped++;
  l->length-=count;
  memmove(l->pending,l->pending+count,l->length*sizeof *l->pending);
}
void pb_rate_loss_enter(PbRateLoss *l,uint64_t sequence) {
  if(sequence==UINT64_MAX) { rate_invalid(l,"missing-frame-key");return; }
  if(l->closed) { rate_invalid(l,"input-after-eos");return; }
  if(l->has_enter && sequence<=l->last_enter) { rate_invalid(l,"nonmonotonic-input-key");return; }
  l->has_enter=true;l->last_enter=sequence;l->entered++;
  if(l->length==PB_LOSS_CAPACITY) { rate_invalid(l,"tracking-overflow");return; }
  l->pending[l->length++]=(PbRateEntry){sequence,0};
}
void pb_rate_loss_exit(PbRateLoss *l,PbFrameKey key) {
  if(!key_valid(key)) { rate_invalid(l,"missing-frame-key");return; }
  if(l->closed) { rate_invalid(l,"output-after-eos");return; }
  if(l->has_exit && (key.sequence<l->last_exit.sequence || key.pts<=l->last_exit.pts)) {
    rate_invalid(l,"nonmonotonic-output-key");return;
  }
  l->has_exit=true;l->last_exit=key;
  for(size_t i=0;i<l->length;i++) {
    if(l->pending[i].sequence!=key.sequence)continue;
    if(l->pending[i].outputs)l->duplicated++;
    l->pending[i].outputs++;l->emitted++;
    rate_retire(l,i);return;
  }
  rate_invalid(l,"output-without-input");
}
void pb_rate_loss_close(PbRateLoss *l) { l->closed=true;rate_retire(l,l->length); }
bool pb_rate_loss_complete(const PbRateLoss *l) {
  return l->closed && !l->invalid_reason && l->length==0 &&
      l->entered+l->duplicated==l->emitted+l->dropped;
}
void pb_cohort_streaming(PbCohortClock *clock,uint64_t now_us) {
  if(clock->streaming)return;
  clock->streaming=true;clock->streaming_us=now_us;
}
int pb_cohort_admit(PbCohortClock *clock,uint64_t now_us) {
  if(clock->ended)return -1;
  if(!clock->streaming || now_us<clock->streaming_us ||
      now_us-clock->streaming_us<clock->warmup_us)return 0;
  if(!clock->started) { clock->started=true;clock->started_us=now_us; }
  if(now_us>=clock->started_us && now_us-clock->started_us>=clock->duration_us) {
    clock->ended=true;clock->ended_us=now_us;return -1;
  }
  return 1;
}
