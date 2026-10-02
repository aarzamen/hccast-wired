/* SPDX-License-Identifier: GPL-3.0-or-later
 * Finite synthetic software test. No capture, receiver, network or config I/O.
 */
#include "pb-wfd-frame-meta.h"
#include <gst/app/gstappsrc.h>
#include <signal.h>
#include <unistd.h>

#define LIMIT 256
#define STAGES 10
#define FRAMES 24
#define EPOCH 7919
typedef struct { guint64 id, pts, offset; } Seen;
typedef struct Fixture Fixture;
typedef struct { Fixture *fixture; guint stage; } Observer;
struct Fixture {
  GMutex lock;
  Seen seen[STAGES][LIMIT];
  guint count[STAGES], supplied, accepted;
  gboolean eos[STAGES], invalid, bus_eos;
  guint fps;
  gboolean timestamp;
};
static const gchar *names[STAGES] = {
  "input:src", "rate:sink", "rate:src", "scale:sink", "scale:src",
  "convert:sink", "convert:src", "queue:sink", "queue:src", "encoder:sink"
};
static guint64 token(guint ordinal) { return 1009 + 37 * (guint64)ordinal; }
static gboolean token_valid(guint64 id) {
  return id >= token(0) && id <= token(FRAMES-1) && (id-token(0))%37 == 0;
}
static void watchdog(int sig) {
  (void)sig;
  const char message[]="FAIL: metadata fixture exceeded 25-second watchdog\n";
  (void)write(STDERR_FILENO,message,sizeof message-1);
  _exit(124);
}
static GstPadProbeReturn observe(GstPad *pad, GstPadProbeInfo *info, gpointer data) {
  Observer *o=data; Fixture *f=o->fixture; (void)pad;
  g_mutex_lock(&f->lock);
  if (GST_PAD_PROBE_INFO_TYPE(info)&GST_PAD_PROBE_TYPE_BUFFER) {
    GstBuffer *buffer=GST_PAD_PROBE_INFO_BUFFER(info);
    const PbFrameMeta *meta=pb_frame_meta_get(buffer);
    if (!meta || meta->epoch!=EPOCH || !token_valid(meta->sequence) || f->count[o->stage]>=LIMIT)
      f->invalid=TRUE;
    else {
      f->seen[o->stage][f->count[o->stage]++]=(Seen){meta->sequence,GST_BUFFER_PTS(buffer),GST_BUFFER_OFFSET(buffer)};
      /* The payload is an independent oracle for lineage at videorate output.
       * A stale valid metadata token must not pass mere set membership. */
      if (o->stage==2) {
        guint8 marker=0;
        if (gst_buffer_extract(buffer,0,&marker,1)!=1 || marker!=16+(meta->sequence-token(0))/37)
          f->invalid=TRUE;
      }
    }
  }
  if ((GST_PAD_PROBE_INFO_TYPE(info)&GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM) &&
      GST_EVENT_TYPE(GST_PAD_PROBE_INFO_EVENT(info))==GST_EVENT_EOS) f->eos[o->stage]=TRUE;
  g_mutex_unlock(&f->lock);
  return GST_PAD_PROBE_OK;
}
static void need_data(GstAppSrc *source, guint bytes, gpointer data) {
  Fixture *f=data; (void)bytes;
  if (f->supplied==FRAMES) { gst_app_src_end_of_stream(source); return; }
  guint ordinal=f->supplied++;
  if (f->timestamp) g_usleep(G_USEC_PER_SEC/f->fps);
  GstBuffer *buffer=gst_buffer_new_allocate(NULL,320*180*4,NULL);
  gst_buffer_memset(buffer,0,16+ordinal,320*180*4);
  if (!pb_frame_meta_add(buffer,EPOCH,token(ordinal))) {
    g_mutex_lock(&f->lock);f->invalid=TRUE;g_mutex_unlock(&f->lock);
  }
  GST_BUFFER_OFFSET(buffer)=token(ordinal);
  GST_BUFFER_OFFSET_END(buffer)=token(ordinal)+1;
  if (!f->timestamp) GST_BUFFER_PTS(buffer)=gst_util_uint64_scale(ordinal,GST_SECOND,f->fps);
  GST_BUFFER_DURATION(buffer)=GST_SECOND/f->fps;
  if (gst_app_src_push_buffer(source,buffer)==GST_FLOW_OK) f->accepted++;
  else { g_mutex_lock(&f->lock);f->invalid=TRUE;g_mutex_unlock(&f->lock); }
}
static gboolean same(Seen a, Seen b) { return a.id==b.id && a.pts==b.pts; }
static gboolean validate(Fixture *f) {
  if (f->invalid || !f->bus_eos || f->accepted!=FRAMES) return FALSE;
  for (guint stage=0;stage<STAGES;stage++) if (!f->eos[stage] || !f->count[stage]) return FALSE;
  if (f->count[0]!=FRAMES || f->count[1]!=FRAMES) return FALSE;
  for (guint i=0;i<FRAMES;i++) {
    if (f->seen[0][i].id!=token(i) || !same(f->seen[0][i],f->seen[1][i])) return FALSE;
  }
  guint distinct=0; guint64 last=0;
  for (guint i=0;i<f->count[2];i++) {
    Seen current=f->seen[2][i];
    if (current.pts==GST_CLOCK_TIME_NONE || (i && current.pts<=f->seen[2][i-1].pts) || current.id<last) return FALSE;
    if (!i || current.id!=last) distinct++;
    last=current.id;
  }
  /* Every output after rate must preserve exact lineage AND its output PTS. */
  for (guint stage=3;stage<STAGES;stage++) {
    if (f->count[stage]!=f->count[2]) return FALSE;
    for (guint i=0;i<f->count[2];i++) if (!same(f->seen[2][i],f->seen[stage][i])) return FALSE;
  }
  if (!f->timestamp && f->fps==30 && (distinct!=FRAMES || f->count[2]!=FRAMES)) return FALSE;
  if (!f->timestamp && f->fps==15 && !(f->count[2]>FRAMES && distinct==FRAMES)) return FALSE;
  if (!f->timestamp && f->fps==60 && !(distinct<FRAMES && distinct==f->count[2])) return FALSE;
  return TRUE;
}
static gboolean copy_test(void) {
  GstBuffer *source=gst_buffer_new_allocate(NULL,8,NULL);
  pb_frame_meta_add(source,EPOCH,token(3));
  GstBuffer *copy=gst_buffer_copy_deep(source);
  GST_BUFFER_OFFSET(copy)=0;
  const PbFrameMeta *meta=pb_frame_meta_get(copy);
  gboolean good=meta && meta->epoch==EPOCH && meta->sequence==token(3);
  GstBuffer *partial=gst_buffer_copy_region(source,GST_BUFFER_COPY_ALL,1,4);
  good=good && !pb_frame_meta_get(partial);
  gst_buffer_unref(source); gst_buffer_unref(copy); gst_buffer_unref(partial);
  return good;
}
static int run_case(guint fps, gboolean timestamp) {
  Fixture f={.fps=fps,.timestamp=timestamp}; Observer observers[STAGES];
  g_mutex_init(&f.lock);
  GError *error=NULL;
  GstElement *pipeline=gst_parse_launch(
      "appsrc name=input ! capsfilter name=inputcaps ! videorate name=rate ! "
      "videoscale name=scale qos=true ! capsfilter name=wirecaps ! "
      "videoconvert name=convert qos=true ! queue name=queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=no ! "
      "x264enc name=encoder tune=zerolatency speed-preset=ultrafast key-int-max=30 bitrate=2000 bframes=0 cabac=false dct8x8=false ! "
      "video/x-h264,profile=baseline ! fakesink sync=false async=false",&error);
  if (!pipeline || error) {
    g_printerr("FAIL: setup: %s\n",error ? error->message : "no pipeline");
    g_clear_error(&error); if(pipeline)gst_object_unref(pipeline); return 1;
  }
  GstElement *source=gst_bin_get_by_name(GST_BIN(pipeline),"input");
  GstElement *inputcaps=gst_bin_get_by_name(GST_BIN(pipeline),"inputcaps");
  GstElement *wirecaps=gst_bin_get_by_name(GST_BIN(pipeline),"wirecaps");
  GstCaps *caps=gst_caps_new_simple("video/x-raw","format",G_TYPE_STRING,"BGRx",
      "width",G_TYPE_INT,320,"height",G_TYPE_INT,180,"framerate",GST_TYPE_FRACTION,fps,1,NULL);
  g_object_set(source,"caps",caps,"is-live",TRUE,"format",GST_FORMAT_TIME,"do-timestamp",timestamp,
      "block",FALSE,"max-buffers",(guint64)1,"max-bytes",(guint64)(320*180*4),"min-latency",(gint64)0,NULL);
  gst_util_set_object_arg(G_OBJECT(source),"leaky-type","downstream");
  g_object_set(inputcaps,"caps",caps,NULL); gst_caps_unref(caps);
  caps=gst_caps_from_string("video/x-raw,width=640,height=360,framerate=30/1");
  g_object_set(wirecaps,"caps",caps,NULL); gst_caps_unref(caps);
  g_signal_connect(source,"need-data",G_CALLBACK(need_data),&f);
  for(guint stage=0;stage<STAGES;stage++) {
    gchar **parts=g_strsplit(names[stage],":",2);
    GstElement *element=gst_bin_get_by_name(GST_BIN(pipeline),parts[0]);
    GstPad *pad=gst_element_get_static_pad(element,parts[1]);
    observers[stage]=(Observer){&f,stage};
    gst_pad_add_probe(pad,GST_PAD_PROBE_TYPE_BUFFER|GST_PAD_PROBE_TYPE_EVENT_DOWNSTREAM,observe,&observers[stage],NULL);
    gst_object_unref(pad); gst_object_unref(element); g_strfreev(parts);
  }
  GstBus *bus=gst_element_get_bus(pipeline);
  gboolean started=gst_element_set_state(pipeline,GST_STATE_PLAYING)!=GST_STATE_CHANGE_FAILURE;
  GstMessage *message=started ? gst_bus_timed_pop_filtered(bus,5*GST_SECOND,GST_MESSAGE_EOS|GST_MESSAGE_ERROR) : NULL;
  if(message && GST_MESSAGE_TYPE(message)==GST_MESSAGE_EOS) f.bus_eos=TRUE;
  else if(message) { gst_message_parse_error(message,&error,NULL); g_printerr("FAIL: pipeline: %s\n",error->message);g_clear_error(&error); }
  if(message)gst_message_unref(message);
  /* The process alarm also bounds a stuck state transition. */
  gst_element_set_state(pipeline,GST_STATE_NULL);
  guint64 rate_drop=0,rate_duplicate=0;
  GstElement *rate=gst_bin_get_by_name(GST_BIN(pipeline),"rate");
  g_object_get(rate,"drop",&rate_drop,"duplicate",&rate_duplicate,NULL);
  gboolean valid=validate(&f);
  g_print("{\"probe\":\"copyable-frame-meta-v1\",\"source_fps\":%u,\"wire_fps\":30,\"do_timestamp\":%s,\"accepted\":%u,\"bus_eos\":%s,\"passed\":%s,\"stages\":{",fps,timestamp?"true":"false",f.accepted,f.bus_eos?"true":"false",valid?"true":"false");
  for(guint stage=0;stage<STAGES;stage++) {
    g_print("%s\"%s\":{\"eos\":%s,\"frames\":[",stage?",":"",names[stage],f.eos[stage]?"true":"false");
    for(guint i=0;i<f.count[stage];i++)g_print("%s[ %" G_GUINT64_FORMAT ", %" G_GUINT64_FORMAT ", %" G_GUINT64_FORMAT " ]",i?",":"",f.seen[stage][i].id,f.seen[stage][i].pts,f.seen[stage][i].offset);
    g_print("]}");
  }
  g_print("}}\n");
  gst_object_unref(rate);gst_object_unref(bus);gst_object_unref(source);gst_object_unref(inputcaps);gst_object_unref(wirecaps);gst_object_unref(pipeline);g_mutex_clear(&f.lock);
  return valid?0:1;
}
int main(int argc,char **argv) {
  signal(SIGALRM,watchdog);alarm(25);gst_init(&argc,&argv);
  g_print("{\"gstreamer\":\"%s\",\"copy_test\":%s}\n",gst_version_string(),copy_test()?"true":"false");
  int failed=!copy_test();
  failed|=run_case(30,FALSE);failed|=run_case(15,FALSE);failed|=run_case(60,FALSE);
  failed|=run_case(30,TRUE);failed|=run_case(15,TRUE);
  alarm(0);return failed;
}
