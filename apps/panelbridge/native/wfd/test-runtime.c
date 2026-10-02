/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdio.h>
#include "pb-wfd-runtime.h"
#define CHECK(expr) do { if (!(expr)) { fprintf(stderr,"FAIL line %d: %s\n",__LINE__,#expr); return 1; } } while (0)
int main(void) {
  CHECK(pb_runtime_rejection(true,false,true,true,false,false)==NULL);
  CHECK(pb_runtime_seconds(true,30)==0);
  CHECK(pb_runtime_seconds(false,30)==30);
  CHECK(pb_runtime_rejection(true,true,true,true,false,false)!=NULL);
  CHECK(pb_runtime_rejection(true,false,false,true,false,false)!=NULL);
  CHECK(pb_runtime_rejection(true,false,true,false,false,false)!=NULL);
  CHECK(pb_runtime_rejection(true,false,true,true,true,false)!=NULL);
  CHECK(pb_runtime_rejection(true,false,true,true,false,true)!=NULL);
  CHECK(pb_runtime_rejection(false,true,false,false,false,true)==NULL);
  CHECK(pb_calibration_rejection(true,true,5000,30000,true,true,false,false,false,false)==NULL);
  CHECK(pb_calibration_rejection(false,false,0,0,false,false,true,true,true,true)==NULL);
  CHECK(pb_calibration_rejection(true,false,5000,30000,true,true,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,4999,30000,true,true,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,5000,29999,true,true,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,60000,14340000,true,true,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,5000,30000,false,true,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,5000,30000,true,false,false,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,5000,30000,true,true,true,false,false,false)!=NULL);
  CHECK(pb_calibration_rejection(true,true,5000,30000,true,true,false,true,false,false)!=NULL);
  PbTiming t={0}; uint64_t age=0;
  CHECK(!pb_timing_oldest_age(&t,100,&age));
  pb_timing_enter(&t,11,100); pb_timing_enter(&t,22,120);
  CHECK(pb_timing_oldest_age(&t,150,&age) && age==50);
  pb_timing_exit(&t,22,170); /* Out-of-order completion must match by identity. */
  pb_timing_exit(&t,11,200);
  CHECK(t.entered==2 && t.exited==2 && t.matched==2);
  CHECK(t.last_us==100 && t.max_us==100 && t.mean_us==75);
  CHECK(!pb_timing_oldest_age(&t,210,&age));
  pb_timing_exit(&t,99,220); CHECK(t.unmatched==1);
  pb_timing_enter(&t,PB_TIMING_NO_KEY,230); CHECK(t.invalid==1);
  pb_timing_enter(&t,33,300); pb_timing_exit(&t,33,299);
  CHECK(t.invalid==2 && t.matched==2); /* Never report negative time. */
  PbTiming bounded={0};
  for (unsigned i=0;i<65;i++) pb_timing_enter(&bounded,i,100+i);
  CHECK(bounded.evicted==1);
  pb_timing_exit(&bounded,0,300); CHECK(bounded.unmatched==1);
  pb_timing_exit(&bounded,64,300); CHECK(bounded.matched==1 && bounded.last_us==136);
  pb_timing_enter(&bounded,1,400); /* Duplicate keys make matching ambiguous. */
  CHECK(bounded.ambiguous);
  pb_timing_exit(&bounded,1,500); CHECK(bounded.matched==1);
  pb_timing_discontinuity(&bounded);
  CHECK(!bounded.ambiguous && bounded.discontinuities==1);
  CHECK(!pb_timing_oldest_age(&bounded,510,&age));
  pb_timing_enter(&bounded,1,600); pb_timing_exit(&bounded,1,610);
  CHECK(bounded.matched==2 && bounded.last_us==10);
  /* Missing accepted IDs are counted once; equal totals cannot hide a drop. */
  PbLoss loss={0};
  pb_loss_enter(&loss,(PbFrameKey){1009,0},true);
  pb_loss_enter(&loss,(PbFrameKey){1046,0},true);
  pb_loss_exit(&loss,(PbFrameKey){1046,0}); /* Observed before push returns. */
  CHECK(!pb_loss_complete(&loss) && loss.dropped==0);
  pb_loss_push_result(&loss,(PbFrameKey){1046,0},true);
  pb_loss_push_result(&loss,(PbFrameKey){1009,0},true);
  CHECK(loss.accepted==2 && loss.emitted==1 && loss.dropped==1);
  pb_loss_close(&loss);
  CHECK(pb_loss_complete(&loss));
  PbLoss flushing={0};
  pb_loss_enter(&flushing,(PbFrameKey){1009,0},true);
  pb_loss_push_result(&flushing,(PbFrameKey){1009,0},false);
  pb_loss_close(&flushing);
  CHECK(pb_loss_complete(&flushing) && flushing.accepted==0 && flushing.dropped==0 && flushing.rejected==1);
  PbLoss bad_push={0};
  pb_loss_enter(&bad_push,(PbFrameKey){1009,0},true);
  pb_loss_exit(&bad_push,(PbFrameKey){1009,0});
  pb_loss_push_result(&bad_push,(PbFrameKey){1009,0},false);
  pb_loss_close(&bad_push); CHECK(!pb_loss_complete(&bad_push));
  PbLoss tail={0};
  pb_loss_enter(&tail,(PbFrameKey){1,0},false);
  CHECK(tail.dropped==0 && !pb_loss_complete(&tail));
  pb_loss_close(&tail);CHECK(pb_loss_complete(&tail) && tail.dropped==1);
  PbLoss unknown_push={0};
  pb_loss_enter(&unknown_push,(PbFrameKey){1,0},true);
  pb_loss_close(&unknown_push);CHECK(!pb_loss_complete(&unknown_push));
  pb_loss_push_result(&unknown_push,(PbFrameKey){1,0},true);
  CHECK(pb_loss_complete(&unknown_push) && unknown_push.dropped==1);
  PbLoss reordered={0};
  pb_loss_enter(&reordered,(PbFrameKey){1,1},false);
  pb_loss_enter(&reordered,(PbFrameKey){2,2},false);
  pb_loss_exit(&reordered,(PbFrameKey){2,2});pb_loss_exit(&reordered,(PbFrameKey){1,1});
  pb_loss_close(&reordered);CHECK(!pb_loss_complete(&reordered));
  PbLoss overflow={0};
  for(unsigned i=0;i<=PB_LOSS_CAPACITY;i++)pb_loss_enter(&overflow,(PbFrameKey){i,0},false);
  pb_loss_close(&overflow);CHECK(!pb_loss_complete(&overflow));
  PbLoss repeated={0};
  pb_loss_enter(&repeated,(PbFrameKey){1,0},false);
  pb_loss_exit(&repeated,(PbFrameKey){1,0});pb_loss_exit(&repeated,(PbFrameKey){1,0});
  pb_loss_close(&repeated);CHECK(!pb_loss_complete(&repeated));
  PbLoss reset={0};
  pb_loss_enter(&reset,(PbFrameKey){1,0},false);pb_loss_invalidate(&reset,"stream-reset");
  pb_loss_close(&reset);CHECK(!pb_loss_complete(&reset));
  /* One rate drop plus one duplicate balances total counts but is still loss. */
  PbRateLoss rate={0};
  pb_rate_loss_enter(&rate,1009);pb_rate_loss_enter(&rate,1046);pb_rate_loss_enter(&rate,1083);
  pb_rate_loss_exit(&rate,(PbFrameKey){1009,0});pb_rate_loss_exit(&rate,(PbFrameKey){1009,33});
  pb_rate_loss_exit(&rate,(PbFrameKey){1083,66});pb_rate_loss_close(&rate);
  CHECK(pb_rate_loss_complete(&rate) && rate.entered==3 && rate.emitted==3 && rate.dropped==1 && rate.duplicated==1);
  PbRateLoss reused_pts={0};
  pb_rate_loss_enter(&reused_pts,1);pb_rate_loss_enter(&reused_pts,2);
  pb_rate_loss_exit(&reused_pts,(PbFrameKey){1,33});pb_rate_loss_exit(&reused_pts,(PbFrameKey){2,33});
  pb_rate_loss_close(&reused_pts);CHECK(!pb_rate_loss_complete(&reused_pts));
  PbRateLoss false_member={0};
  pb_rate_loss_enter(&false_member,1009);pb_rate_loss_exit(&false_member,(PbFrameKey){0,0});
  pb_rate_loss_close(&false_member);CHECK(!pb_rate_loss_complete(&false_member));
  /* Capacity bounds pending work, not total frames in a long run. */
  PbLoss long_run={0};PbRateLoss long_rate={0};
  for(unsigned i=0;i<100000;i++) {
    pb_loss_enter(&long_run,(PbFrameKey){i,0},false);pb_loss_exit(&long_run,(PbFrameKey){i,0});
    pb_rate_loss_enter(&long_rate,i);pb_rate_loss_exit(&long_rate,(PbFrameKey){i,i});
  }
  pb_loss_close(&long_run);pb_rate_loss_close(&long_rate);
  CHECK(pb_loss_complete(&long_run) && long_run.accepted==100000 && long_run.dropped==0);
  CHECK(pb_rate_loss_complete(&long_rate) && long_rate.entered==100000 && long_rate.dropped==0);
  PbCohortClock window={.warmup_us=5000000,.duration_us=30000000};
  CHECK(pb_cohort_admit(&window,100)==0 && !window.started);
  pb_cohort_streaming(&window,1000);pb_cohort_streaming(&window,2000);
  CHECK(window.streaming_us==1000);
  CHECK(pb_cohort_admit(&window,5000999)==0);
  CHECK(pb_cohort_admit(&window,5001500)==1 && window.started_us==5001500);
  CHECK(pb_cohort_admit(&window,35001499)==1);
  CHECK(pb_cohort_admit(&window,35002500)==-1 && window.ended_us==35002500);
  CHECK(pb_cohort_admit(&window,36000000)==-1 && window.ended_us==35002500);
  puts("PASS: continuous admission and bounded monotonic timing correlation");
  return 0;
}
