/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "pb-wfd-telemetry.h"
#include "pb-wfd-runtime.h"
#include "pb-wfd-frame-meta.h"
#include <json-glib/json-glib.h>
#include <stdint.h>
#include <string.h>

typedef enum { SOURCE, RATE_IN, RATE_OUT, SCALE_IN, SCALE_OUT, CONVERT_IN,
  CONVERT_OUT, QUEUE_IN, QUEUE_OUT, ENCODER_IN, ENCODER_OUT, PROBE_COUNT } ProbeKind;
typedef enum { APP, SOURCE_RATE, RATE_SCALE, SCALE, SCALE_CONVERT, CONVERT,
  CONVERT_QUEUE, QUEUE, QUEUE_ENCODER, BOUNDARY_COUNT } Boundary;
static const char *boundary_names[BOUNDARY_COUNT]={"appsrc","source_to_rate","rate_to_scale",
  "scale","scale_to_convert","convert","convert_to_queue","queue","queue_to_encoder"};
typedef struct { ProbeKind kind; GstSegment segment; gboolean segment_valid, eos; GstCaps *caps; GstPad *pad; gulong id; } Probe;
static struct {
  GMutex lock;
  Probe probes[PROBE_COUNT];
  GstElement *input, *rate, *queue;
  PbTiming queue_timing, encoder_timing;
  guint64 source_buffers, raw_pushed, encoded_buffers, encoded_bytes, encoded_frames, unknown_encoded;
  guint64 rate_dropped, rate_duplicated, appsrc_dropped;
  gboolean encoder_registered, encoder_attached, alignment_au, appsrc_drop_known;
  gint64 start_us, previous_us;
  guint64 previous_source, previous_encoded, previous_bytes;
  struct {
    gboolean enabled, closed;
    PbCohortClock clock;
    guint64 next_sequence, first_sequence, end_sequence, closed_us;
    PbLoss boundaries[BOUNDARY_COUNT];
    PbRateLoss rate;
    PbTiming encoder_timing;
    guint64 encoded_frames, unknown_encoded;
    gboolean encoder_output_key_unknown;
    const char *invalid_reason;
  } cohort;
} metrics;

static void invalidate_cohort(const char *reason) {
  if(metrics.cohort.enabled && !metrics.cohort.invalid_reason)metrics.cohort.invalid_reason=reason;
}
static void cohort_buffer(Probe *probe,GstBuffer *buffer) {
  if(!metrics.cohort.enabled || !metrics.cohort.clock.started || probe->kind==ENCODER_OUT)return;
  const PbFrameMeta *meta=pb_frame_meta_get(buffer);
  if(!meta) { invalidate_cohort("missing-frame-metadata");return; }
  /* Both epoch and source range identify warmup. A cohort frame whose tag was
   * rewritten to zero must not silently become a discarded warmup frame. */
  if(meta->epoch==0 && meta->sequence<metrics.cohort.first_sequence)return;
  if(meta->epoch!=metrics.cohort.clock.started_us) { invalidate_cohort("wrong-cohort-epoch");return; }
  PbFrameKey key={meta->sequence,probe->kind<=RATE_IN?0:GST_BUFFER_PTS(buffer)};
  PbLoss *b=metrics.cohort.boundaries;
  switch(probe->kind) {
    case SOURCE:pb_loss_exit(&b[APP],key);pb_loss_enter(&b[SOURCE_RATE],key,false);break;
    case RATE_IN:pb_loss_exit(&b[SOURCE_RATE],key);pb_rate_loss_enter(&metrics.cohort.rate,key.sequence);break;
    case RATE_OUT:pb_rate_loss_exit(&metrics.cohort.rate,key);pb_loss_enter(&b[RATE_SCALE],key,false);break;
    case SCALE_IN:pb_loss_exit(&b[RATE_SCALE],key);pb_loss_enter(&b[SCALE],key,false);break;
    case SCALE_OUT:pb_loss_exit(&b[SCALE],key);pb_loss_enter(&b[SCALE_CONVERT],key,false);break;
    case CONVERT_IN:pb_loss_exit(&b[SCALE_CONVERT],key);pb_loss_enter(&b[CONVERT],key,false);break;
    case CONVERT_OUT:pb_loss_exit(&b[CONVERT],key);pb_loss_enter(&b[CONVERT_QUEUE],key,false);break;
    case QUEUE_IN:pb_loss_exit(&b[CONVERT_QUEUE],key);pb_loss_enter(&b[QUEUE],key,false);break;
    case QUEUE_OUT:pb_loss_exit(&b[QUEUE],key);pb_loss_enter(&b[QUEUE_ENCODER],key,false);break;
    case ENCODER_IN:pb_loss_exit(&b[QUEUE_ENCODER],key);break;
    default:break;
  }
}
static void cohort_eos(ProbeKind kind,guint64 now) {
  if(!metrics.cohort.enabled)return;
  PbLoss *b=metrics.cohort.boundaries;
  switch(kind) {
    case SOURCE:pb_loss_close(&b[APP]);break;
    case RATE_IN:pb_loss_close(&b[SOURCE_RATE]);break;
    case RATE_OUT:pb_rate_loss_close(&metrics.cohort.rate);break;
    case SCALE_IN:pb_loss_close(&b[RATE_SCALE]);break;
    case SCALE_OUT:pb_loss_close(&b[SCALE]);break;
    case CONVERT_IN:pb_loss_close(&b[SCALE_CONVERT]);break;
    case CONVERT_OUT:pb_loss_close(&b[CONVERT]);break;
    case QUEUE_IN:pb_loss_close(&b[CONVERT_QUEUE]);break;
    case QUEUE_OUT:pb_loss_close(&b[QUEUE]);break;
    case ENCODER_IN:
      pb_loss_close(&b[QUEUE_ENCODER]);metrics.cohort.closed=TRUE;metrics.cohort.closed_us=now;break;
    default:break;
  }
}
static const char *cohort_problem(void) {
  if(!metrics.cohort.enabled)return "disabled";
  if(metrics.cohort.invalid_reason)return metrics.cohort.invalid_reason;
  for(guint i=0;i<BOUNDARY_COUNT;i++)if(metrics.cohort.boundaries[i].invalid_reason)
    return metrics.cohort.boundaries[i].invalid_reason;
  if(metrics.cohort.rate.invalid_reason)return metrics.cohort.rate.invalid_reason;
  for(guint i=0;i<PROBE_COUNT;i++)if(!metrics.probes[i].pad)return "required-probe-unavailable";
  if(!metrics.cohort.clock.started)return "cohort-not-started";
  if(!metrics.cohort.clock.ended)return "source-window-not-ended";
  if(!metrics.cohort.closed)return "encoder-sink-eos-pending";
  if(!pb_rate_loss_complete(&metrics.cohort.rate))return "rate-ledger-unsettled";
  for(guint i=0;i<BOUNDARY_COUNT;i++)if(!pb_loss_complete(&metrics.cohort.boundaries[i]))return "boundary-ledger-unsettled";
  guint64 dropped=metrics.cohort.rate.dropped;
  for(guint i=0;i<BOUNDARY_COUNT;i++)dropped+=metrics.cohort.boundaries[i].dropped;
  if(metrics.cohort.boundaries[APP].accepted+metrics.cohort.rate.duplicated!=
      metrics.cohort.boundaries[QUEUE_ENCODER].emitted+dropped)return "unbalanced-cohort";
  return NULL;
}

static void count_buffer(Probe *probe, GstBuffer *buffer, guint64 now) {
  guint64 key;
  cohort_buffer(probe,buffer);
  switch (probe->kind) {
    case SOURCE: metrics.source_buffers++; break;
    case QUEUE_IN:
      pb_timing_enter(&metrics.queue_timing,(guint64)(uintptr_t)buffer,now); break;
    case QUEUE_OUT:
      pb_timing_exit(&metrics.queue_timing,(guint64)(uintptr_t)buffer,now); break;
    case ENCODER_IN:
    case ENCODER_OUT:
      key=probe->segment_valid && GST_BUFFER_PTS_IS_VALID(buffer) ?
          gst_segment_to_running_time(&probe->segment,GST_FORMAT_TIME,GST_BUFFER_PTS(buffer)) : GST_CLOCK_TIME_NONE;
      if (probe->kind==ENCODER_IN) {
        pb_timing_enter(&metrics.encoder_timing,key,now);
        const PbFrameMeta *meta=pb_frame_meta_get(buffer);
        if(metrics.cohort.enabled && metrics.cohort.clock.started && meta &&
            meta->epoch==metrics.cohort.clock.started_us)
          pb_timing_enter(&metrics.cohort.encoder_timing,key,now);
      }
      else {
        gboolean input_key_known=FALSE;
        for(guint i=0;i<PB_TIMING_CAPACITY;i++)
          if(metrics.encoder_timing.pending[i].used && metrics.encoder_timing.pending[i].key==key)
            input_key_known=TRUE;
        metrics.encoded_buffers++;
        metrics.encoded_bytes+=gst_buffer_get_size(buffer);
        if (metrics.alignment_au) metrics.encoded_frames++;
        else metrics.unknown_encoded++;
        pb_timing_exit(&metrics.encoder_timing,key,now);
        if(metrics.cohort.enabled && metrics.cohort.clock.started) {
          if(key==GST_CLOCK_TIME_NONE || !input_key_known)metrics.cohort.encoder_output_key_unknown=TRUE;
          else for(guint i=0;i<PB_TIMING_CAPACITY;i++) {
            PbTimingEntry *entry=&metrics.cohort.encoder_timing.pending[i];
            if(entry->used && entry->key==key) {
              if(metrics.alignment_au)metrics.cohort.encoded_frames++;
              else metrics.cohort.unknown_encoded++;
              pb_timing_exit(&metrics.cohort.encoder_timing,key,now);
              break;
            }
          }
        }
      }
      break;
    default: break;
  }
}

static GstPadProbeReturn observe(GstPad *pad, GstPadProbeInfo *info, gpointer data) {
  Probe *probe=data;
  (void)pad;
  g_mutex_lock(&metrics.lock);
  if (GST_PAD_PROBE_INFO_TYPE(info)&(GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM|GST_PAD_PROBE_TYPE_EVENT_FLUSH)) {
    GstEvent *event=GST_PAD_PROBE_INFO_EVENT(info);
    if (GST_EVENT_TYPE(event)==GST_EVENT_SEGMENT) {
      const GstSegment *segment;
      gst_event_parse_segment(event,&segment);
      if(probe->segment_valid && metrics.cohort.clock.started && !metrics.cohort.closed)
        invalidate_cohort("segment-changed-during-cohort");
      probe->segment=*segment;
      probe->segment_valid=segment->format==GST_FORMAT_TIME;
      if (probe->kind==ENCODER_IN) pb_timing_discontinuity(&metrics.encoder_timing);
      if (probe->kind==QUEUE_IN) pb_timing_discontinuity(&metrics.queue_timing);
    } else if (GST_EVENT_TYPE(event)==GST_EVENT_FLUSH_STOP) {
      if(metrics.cohort.clock.started && !metrics.cohort.closed)invalidate_cohort("flush-during-cohort");
      probe->segment_valid=FALSE;
      if (probe->kind==ENCODER_IN) pb_timing_discontinuity(&metrics.encoder_timing);
      if (probe->kind==QUEUE_IN) pb_timing_discontinuity(&metrics.queue_timing);
    } else if (GST_EVENT_TYPE(event)==GST_EVENT_EOS) {
      probe->eos=TRUE;cohort_eos(probe->kind,(guint64)g_get_monotonic_time());
    } else if (GST_EVENT_TYPE(event)==GST_EVENT_CAPS) {
      GstCaps *caps;
      gst_event_parse_caps(event,&caps);
      if(probe->caps && !gst_caps_is_equal(probe->caps,caps) &&
          metrics.cohort.clock.started && !metrics.cohort.closed)
        invalidate_cohort("caps-changed-during-cohort");
      gst_caps_replace(&probe->caps,caps);
      if(probe->kind==ENCODER_OUT) {
        const GstStructure *structure=gst_caps_get_structure(caps,0);
        metrics.alignment_au=g_strcmp0(gst_structure_get_string(structure,"alignment"),"au")==0;
      }
    }
  }
  if (GST_PAD_PROBE_INFO_TYPE(info)&GST_PAD_PROBE_TYPE_BUFFER)
    count_buffer(probe,GST_PAD_PROBE_INFO_BUFFER(info),(guint64)g_get_monotonic_time());
  else if (GST_PAD_PROBE_INFO_TYPE(info)&GST_PAD_PROBE_TYPE_BUFFER_LIST) {
    GstBufferList *list=GST_PAD_PROBE_INFO_BUFFER_LIST(info);
    for (guint i=0;i<gst_buffer_list_length(list);i++)
      count_buffer(probe,gst_buffer_list_get(list,i),(guint64)g_get_monotonic_time());
  }
  g_mutex_unlock(&metrics.lock);
  return GST_PAD_PROBE_OK;
}

/* Called before the pipeline starts. Exactly one source/encoder per worker. */
static void attach(GstElement *element, const gchar *pad_name, ProbeKind kind) {
  Probe *probe=&metrics.probes[kind];
  if (!element) return;
  GstPad *pad=gst_element_get_static_pad(element,pad_name);
  if (!pad) return;
  g_mutex_lock(&metrics.lock);
  if (probe->pad) {
    g_mutex_unlock(&metrics.lock); gst_object_unref(pad); return;
  }
  probe->pad=pad;
  probe->kind=kind;
  probe->segment_valid=FALSE;probe->eos=FALSE;
  gst_segment_init(&probe->segment,GST_FORMAT_TIME);
  g_mutex_unlock(&metrics.lock);
  gulong id=gst_pad_add_probe(pad,GST_PAD_PROBE_TYPE_BUFFER|GST_PAD_PROBE_TYPE_BUFFER_LIST|
      GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM|GST_PAD_PROBE_TYPE_EVENT_FLUSH,observe,probe,NULL);
  g_mutex_lock(&metrics.lock); probe->id=id; g_mutex_unlock(&metrics.lock);
}

void pb_wfd_telemetry_attach_source(GstElement *source, GstElement *rate) {
  g_mutex_lock(&metrics.lock);
  if (!metrics.input) metrics.input=gst_object_ref(source);
  if (!metrics.rate) metrics.rate=gst_object_ref(rate);
  if (!metrics.start_us) metrics.start_us=g_get_monotonic_time();
  g_mutex_unlock(&metrics.lock);
  attach(source,"src",SOURCE);
  if(metrics.cohort.enabled) { attach(rate,"sink",RATE_IN);attach(rate,"src",RATE_OUT); }
}

void pb_wfd_telemetry_attach_encoder(GstElement *encoder) {
  g_mutex_lock(&metrics.lock);
  if (metrics.encoder_registered) { g_mutex_unlock(&metrics.lock); return; }
  metrics.encoder_registered=TRUE;
  g_mutex_unlock(&metrics.lock);
  attach(encoder,"sink",ENCODER_IN);
  attach(encoder,"src",ENCODER_OUT);
  g_autoptr(GstObject) parent=gst_object_get_parent(GST_OBJECT(encoder));
  GstElement *queue=parent && GST_IS_BIN(parent) ? gst_bin_get_by_name(GST_BIN(parent),"wfd-pre-encoder-queue") : NULL;
  attach(queue,"sink",QUEUE_IN);
  attach(queue,"src",QUEUE_OUT);
  if(metrics.cohort.enabled && parent && GST_IS_BIN(parent)) {
    GstElement *scale=gst_bin_get_by_name(GST_BIN(parent),"wfd-scale");
    GstElement *convert=gst_bin_get_by_name(GST_BIN(parent),"wfd-videoconvert");
    attach(scale,"sink",SCALE_IN);attach(scale,"src",SCALE_OUT);
    attach(convert,"sink",CONVERT_IN);attach(convert,"src",CONVERT_OUT);
    gst_clear_object(&scale);gst_clear_object(&convert);
  }
  g_mutex_lock(&metrics.lock);
  metrics.queue=queue;
  metrics.encoder_attached=metrics.probes[ENCODER_IN].pad && metrics.probes[ENCODER_OUT].pad;
  g_mutex_unlock(&metrics.lock);
}

void pb_wfd_telemetry_raw_pushed(void) {
  g_mutex_lock(&metrics.lock); metrics.raw_pushed++; g_mutex_unlock(&metrics.lock);
}
void pb_wfd_telemetry_calibration_configure(guint warmup_ms,guint duration_ms) {
  g_mutex_lock(&metrics.lock);
  memset(&metrics.cohort,0,sizeof metrics.cohort);
  metrics.cohort.enabled=TRUE;
  metrics.cohort.clock.warmup_us=(guint64)warmup_ms*1000;
  metrics.cohort.clock.duration_us=(guint64)duration_ms*1000;
  g_mutex_unlock(&metrics.lock);
}
void pb_wfd_telemetry_calibration_streaming(void) {
  g_mutex_lock(&metrics.lock);
  if(metrics.cohort.enabled)pb_cohort_streaming(&metrics.cohort.clock,(guint64)g_get_monotonic_time());
  g_mutex_unlock(&metrics.lock);
}
gboolean pb_wfd_telemetry_raw_begin(GstBuffer *buffer,guint64 *sequence) {
  g_mutex_lock(&metrics.lock);
  *sequence=0;
  if(!metrics.cohort.enabled) { g_mutex_unlock(&metrics.lock);return TRUE; }
  gboolean was_started=metrics.cohort.clock.started;
  gboolean was_ended=metrics.cohort.clock.ended;
  int admission=pb_cohort_admit(&metrics.cohort.clock,(guint64)g_get_monotonic_time());
  if(admission<0) {
    if(!was_ended)metrics.cohort.end_sequence=metrics.cohort.next_sequence;
    g_mutex_unlock(&metrics.lock);return FALSE;
  }
  *sequence=metrics.cohort.next_sequence++;
  if(!was_started && metrics.cohort.clock.started)metrics.cohort.first_sequence=*sequence;
  guint64 epoch=admission?metrics.cohort.clock.started_us:0;
  if(!pb_frame_meta_add(buffer,epoch,*sequence))invalidate_cohort("cannot-stamp-frame-metadata");
  if(admission)pb_loss_enter(&metrics.cohort.boundaries[APP],(PbFrameKey){*sequence,0},true);
  g_mutex_unlock(&metrics.lock);return TRUE;
}
void pb_wfd_telemetry_raw_result(guint64 sequence,GstFlowReturn flow) {
  g_mutex_lock(&metrics.lock);
  if(metrics.cohort.enabled && metrics.cohort.clock.started && sequence>=metrics.cohort.first_sequence &&
      (!metrics.cohort.clock.ended || sequence<metrics.cohort.end_sequence)) {
    pb_loss_push_result(&metrics.cohort.boundaries[APP],(PbFrameKey){sequence,0},flow==GST_FLOW_OK);
    if(flow!=GST_FLOW_OK)invalidate_cohort("cohort-push-not-accepted");
  }
  g_mutex_unlock(&metrics.lock);
}
gboolean pb_wfd_telemetry_calibration_closed(gboolean *complete) {
  g_mutex_lock(&metrics.lock);
  *complete=cohort_problem()==NULL;
  gboolean ready=metrics.cohort.closed && metrics.probes[ENCODER_OUT].eos;
  g_mutex_unlock(&metrics.lock);return ready;
}
void pb_wfd_telemetry_calibration_abort(const char *reason) {
  g_mutex_lock(&metrics.lock);invalidate_cohort(reason);g_mutex_unlock(&metrics.lock);
}
guint64 pb_wfd_telemetry_raw_frames(void) {
  g_mutex_lock(&metrics.lock); guint64 count=metrics.raw_pushed; g_mutex_unlock(&metrics.lock); return count;
}

static void add_int(JsonBuilder *b,const char *name,guint64 value) {
  json_builder_set_member_name(b,name); json_builder_add_int_value(b,(gint64)value);
}
static void add_bool(JsonBuilder *b,const char *name,gboolean value) {
  json_builder_set_member_name(b,name); json_builder_add_boolean_value(b,value);
}
static void add_null(JsonBuilder *b,const char *name) {
  json_builder_set_member_name(b,name); json_builder_add_null_value(b);
}
static void add_double(JsonBuilder *b,const char *name,double value) {
  json_builder_set_member_name(b,name); json_builder_add_double_value(b,value);
}
static void add_timing(JsonBuilder *b,const char *name,const PbTiming *t,guint64 now,gboolean available) {
  json_builder_set_member_name(b,name); json_builder_begin_object(b);
  add_bool(b,"available",available);
  add_int(b,"entered",t->entered); add_int(b,"exited",t->exited);
  add_int(b,"matched",t->matched); add_int(b,"unmatched",t->unmatched);
  add_int(b,"tracking_evicted",t->evicted); add_int(b,"invalid",t->invalid);
  add_int(b,"discontinuities",t->discontinuities); add_bool(b,"ambiguous",t->ambiguous);
  if (t->matched) {
    add_int(b,"last_us",t->last_us); add_int(b,"max_us",t->max_us); add_double(b,"mean_us",t->mean_us);
  } else { add_null(b,"last_us"); add_null(b,"max_us"); add_null(b,"mean_us"); }
  guint64 age;
  if (pb_timing_oldest_age(t,now,&age)) add_int(b,"oldest_pending_age_us",age);
  else add_null(b,"oldest_pending_age_us");
  json_builder_end_object(b);
}
static void add_cohort(JsonBuilder *b,guint64 now) {
  json_builder_set_member_name(b,"local_frame_accounting");json_builder_begin_object(b);
  add_int(b,"version",1);add_bool(b,"enabled",metrics.cohort.enabled);
  json_builder_set_member_name(b,"scope");json_builder_add_string_value(b,"accepted-source-to-encoder-sink");
  const char *problem=cohort_problem();
  add_bool(b,"complete",problem==NULL);add_bool(b,"closed",metrics.cohort.closed);
  add_int(b,"requested_warmup_us",metrics.cohort.clock.warmup_us);
  add_int(b,"requested_duration_us",metrics.cohort.clock.duration_us);
  if(metrics.cohort.clock.streaming)add_int(b,"streaming_us",metrics.cohort.clock.streaming_us);
  else add_null(b,"streaming_us");
  json_builder_set_member_name(b,"invalid_reason");
  if(problem)json_builder_add_string_value(b,problem);else json_builder_add_null_value(b);
  if(metrics.cohort.clock.started) {
    add_int(b,"epoch",metrics.cohort.clock.started_us);
    add_int(b,"started_us",metrics.cohort.clock.started_us);
    add_int(b,"source_seq_first",metrics.cohort.first_sequence);
  } else {add_null(b,"epoch");add_null(b,"started_us");add_null(b,"source_seq_first");}
  if(metrics.cohort.clock.ended) {
    add_int(b,"ended_us",metrics.cohort.clock.ended_us);
    add_int(b,"source_seq_end_exclusive",metrics.cohort.end_sequence);
  } else {add_null(b,"ended_us");add_null(b,"source_seq_end_exclusive");}
  if(metrics.cohort.closed)add_int(b,"closed_us",metrics.cohort.closed_us);else add_null(b,"closed_us");
  add_bool(b,"encoder_src_eos_seen",metrics.probes[ENCODER_OUT].eos);
  add_int(b,"accepted_source_frames",metrics.cohort.boundaries[APP].accepted);
  add_int(b,"rejected_source_frames",metrics.cohort.boundaries[APP].rejected);
  add_int(b,"emitted_wire_frames",metrics.cohort.boundaries[QUEUE_ENCODER].emitted);
  if(metrics.alignment_au && !metrics.cohort.unknown_encoded &&
      !metrics.cohort.encoder_output_key_unknown && !metrics.cohort.encoder_timing.ambiguous &&
      !metrics.cohort.encoder_timing.evicted && !metrics.cohort.encoder_timing.invalid)
    add_int(b,"encoded_frames",metrics.cohort.encoded_frames);
  else add_null(b,"encoded_frames");
  add_bool(b,"encoder_output_key_unknown",metrics.cohort.encoder_output_key_unknown);
  add_timing(b,"encoder_sink_to_src_us",&metrics.cohort.encoder_timing,now,
      metrics.cohort.enabled && metrics.encoder_attached && !metrics.cohort.encoder_output_key_unknown);
  add_int(b,"rate_source_frames_dropped",metrics.cohort.rate.dropped);
  add_int(b,"rate_frames_duplicated",metrics.cohort.rate.duplicated);
  guint64 dropped=metrics.cohort.rate.dropped;
  json_builder_set_member_name(b,"boundary_frames_dropped");json_builder_begin_object(b);
  for(guint i=0;i<BOUNDARY_COUNT;i++) {
    add_int(b,boundary_names[i],metrics.cohort.boundaries[i].dropped);
    dropped+=metrics.cohort.boundaries[i].dropped;
  }
  json_builder_end_object(b);
  if(!problem)add_int(b,"pipeline_frames_dropped",dropped);else add_null(b,"pipeline_frames_dropped");
  json_builder_end_object(b);
}

/* Sample upstream properties outside our lock: getters may lock their elements. */
void pb_wfd_telemetry_emit(gboolean final) {
  GstElement *input=NULL,*rate=NULL,*queue=NULL;
  g_mutex_lock(&metrics.lock);
  if (metrics.input) input=gst_object_ref(metrics.input);
  if (metrics.rate) rate=gst_object_ref(metrics.rate);
  if (metrics.queue) queue=gst_object_ref(metrics.queue);
  g_mutex_unlock(&metrics.lock);
  guint64 drop=0,duplicate=0,input_drop=0,queue_time=0;
  guint queue_buffers=0;
  gboolean input_drop_known=input && g_object_class_find_property(G_OBJECT_GET_CLASS(input),"dropped");
  if (rate) g_object_get(rate,"drop",&drop,"duplicate",&duplicate,NULL);
  if (input_drop_known) g_object_get(input,"dropped",&input_drop,NULL);
  if (queue) g_object_get(queue,"current-level-time",&queue_time,"current-level-buffers",&queue_buffers,NULL);
  g_autoptr(JsonBuilder) b=json_builder_new();
  g_autoptr(JsonGenerator) generator=json_generator_new();
  gint64 now=g_get_monotonic_time();
  json_builder_begin_object(b);
  json_builder_set_member_name(b,"event"); json_builder_add_string_value(b,"pipeline-telemetry");
  add_int(b,"metrics_version",1); add_int(b,"monotonic_us",now); add_bool(b,"final",final);
  g_mutex_lock(&metrics.lock);
  if (!metrics.start_us) metrics.start_us=now;
  add_int(b,"elapsed_us",now-metrics.start_us);
  add_bool(b,"source_probe_available",metrics.probes[SOURCE].pad!=NULL);
  add_int(b,"source_buffers",metrics.source_buffers);
  add_int(b,"raw_frames_pushed",metrics.raw_pushed);
  add_bool(b,"encoder_probes_available",metrics.encoder_attached);
  add_int(b,"encoder_input_buffers",metrics.encoder_timing.entered);
  add_int(b,"encoded_buffers",metrics.encoded_buffers);
  add_int(b,"encoded_bytes",metrics.encoded_bytes);
  if (metrics.alignment_au && metrics.unknown_encoded==0) add_int(b,"encoded_frames",metrics.encoded_frames);
  else add_null(b,"encoded_frames");
  add_int(b,"encoded_buffers_with_unknown_frame_count",metrics.unknown_encoded);
  if (metrics.previous_us && now>metrics.previous_us) {
    double seconds=(now-metrics.previous_us)/1000000.0;
    add_int(b,"sample_interval_us",now-metrics.previous_us);
    add_double(b,"source_buffers_per_second",(metrics.source_buffers-metrics.previous_source)/seconds);
    add_double(b,"encoded_buffers_per_second",(metrics.encoded_buffers-metrics.previous_encoded)/seconds);
    add_double(b,"encoded_output_kbit_per_second",(metrics.encoded_bytes-metrics.previous_bytes)*8.0/1000.0/seconds);
  } else {
    add_null(b,"sample_interval_us"); add_null(b,"source_buffers_per_second");
    add_null(b,"encoded_buffers_per_second"); add_null(b,"encoded_output_kbit_per_second");
  }
  metrics.previous_us=now; metrics.previous_source=metrics.source_buffers;
  metrics.previous_encoded=metrics.encoded_buffers; metrics.previous_bytes=metrics.encoded_bytes;
  metrics.rate_dropped=MAX(metrics.rate_dropped,drop);
  metrics.rate_duplicated=MAX(metrics.rate_duplicated,duplicate);
  metrics.appsrc_dropped=MAX(metrics.appsrc_dropped,input_drop);
  metrics.appsrc_drop_known |= input_drop_known;
  if (rate) {
    add_int(b,"videorate_frames_dropped",metrics.rate_dropped);
    add_int(b,"videorate_frames_duplicated",metrics.rate_duplicated);
  } else { add_null(b,"videorate_frames_dropped"); add_null(b,"videorate_frames_duplicated"); }
  if (metrics.appsrc_drop_known) add_int(b,"appsrc_frames_dropped",metrics.appsrc_dropped);
  else add_null(b,"appsrc_frames_dropped");
  if (queue) {
    add_int(b,"pre_encoder_queue_buffers",queue_buffers);
    add_int(b,"pre_encoder_queue_media_duration_ns",queue_time);
  } else { add_null(b,"pre_encoder_queue_buffers"); add_null(b,"pre_encoder_queue_media_duration_ns"); }
  add_timing(b,"pre_encoder_queue_residence_us",&metrics.queue_timing,now,queue!=NULL);
  add_timing(b,"encoder_sink_to_src_us",&metrics.encoder_timing,now,metrics.encoder_attached);
  add_cohort(b,now);
  g_mutex_unlock(&metrics.lock);
  add_null(b,"other_pipeline_frames_dropped"); add_null(b,"transport_frames_dropped");
  add_null(b,"receiver_frames_dropped"); add_null(b,"panel_latency_us");
  json_builder_set_member_name(b,"evidence");
  json_builder_add_string_value(b,"local-pipeline-observations; transport-delivery-and-panel-latency-unknown");
  json_builder_end_object(b);
  JsonNode *root=json_builder_get_root(b);
  json_generator_set_root(generator,root);
  g_autofree gchar *line=json_generator_to_data(generator,NULL);
  g_print("%s\n",line);
  json_node_free(root);
  gst_clear_object(&input); gst_clear_object(&rate); gst_clear_object(&queue);
}

/* The worker calls this after stopping the pipeline and removing the timer. */
void pb_wfd_telemetry_clear(void) {
  for (guint i=0;i<PROBE_COUNT;i++) {
    Probe *probe=&metrics.probes[i];
    if (probe->pad) {
      if (probe->id) gst_pad_remove_probe(probe->pad,probe->id);
      gst_clear_object(&probe->pad);
    }
    gst_clear_caps(&probe->caps);
  }
  gst_clear_object(&metrics.input); gst_clear_object(&metrics.rate); gst_clear_object(&metrics.queue);
  /* No producer/probe/timer remains. Clear references and per-source state so
   * an independent software fixture/source instance cannot inherit old IDs. */
  memset(metrics.probes,0,sizeof metrics.probes);
  metrics.encoder_registered=FALSE;metrics.encoder_attached=FALSE;
  memset(&metrics.cohort,0,sizeof metrics.cohort);
}
