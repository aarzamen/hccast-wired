/* SPDX-License-Identifier: GPL-3.0-or-later
 * Actual telemetry on generated raw frames. No receiver/capture/network access.
 * Small internal windows test bookkeeping; they never qualify a preset.
 */
#include "pb-wfd-telemetry.h"
#include "pb-wfd-frame-meta.h"
#include <gst/app/gstappsrc.h>
#include <signal.h>
#include <unistd.h>
typedef struct { guint fps, ordinal; gboolean burst, drop, strip, zero_epoch, injected, failed; } Fixture;
static void watchdog(int signal_number) {
  (void)signal_number;
  const char message[]="FAIL: calibration fixture watchdog\n";
  (void)write(STDERR_FILENO,message,sizeof message-1);_exit(124);
}
static void need_data(GstAppSrc *source,guint bytes,gpointer data) {
  Fixture *f=data;(void)bytes;g_usleep(G_USEC_PER_SEC/f->fps);
  for(guint i=0;i<(f->burst?3u:1u);i++) {
    GstBuffer *buffer=gst_buffer_new_allocate(NULL,64*48*4,NULL);
    gst_buffer_memset(buffer,0,32+(f->ordinal++%128),64*48*4);
    GST_BUFFER_DURATION(buffer)=GST_SECOND/f->fps;
    guint64 sequence=0;
    if(!pb_wfd_telemetry_raw_begin(buffer,&sequence)) {
      gst_buffer_unref(buffer);gst_app_src_end_of_stream(source);return;
    }
    GstFlowReturn flow=gst_app_src_push_buffer(source,buffer);
    pb_wfd_telemetry_raw_result(sequence,flow);
    if(flow==GST_FLOW_OK)pb_wfd_telemetry_raw_pushed();else f->failed=TRUE;
  }
}
static GstPadProbeReturn inject(GstPad *pad,GstPadProbeInfo *info,gpointer data) {
  Fixture *f=data;(void)pad;
  GstBuffer *buffer=GST_PAD_PROBE_INFO_BUFFER(info);
  const PbFrameMeta *meta=pb_frame_meta_get(buffer);
  if(!meta || !meta->epoch || f->injected)return GST_PAD_PROBE_OK;
  f->injected=TRUE;
  if(f->drop)return GST_PAD_PROBE_DROP;
  buffer=gst_buffer_make_writable(buffer);
  GST_PAD_PROBE_INFO_DATA(info)=buffer;
  meta=pb_frame_meta_get(buffer);
  if(f->zero_epoch)((PbFrameMeta *)meta)->epoch=0;
  else gst_buffer_remove_meta(buffer,(GstMeta *)meta);
  return GST_PAD_PROBE_OK;
}
static int run_case(guint fps,gboolean burst,gboolean drop,gboolean strip,gboolean zero_epoch) {
  Fixture f={.fps=fps,.burst=burst,.drop=drop,.strip=strip,.zero_epoch=zero_epoch};GError *error=NULL;
  GstElement *pipeline=gst_parse_launch(
      "appsrc name=panelbridge-input ! videorate name=panelbridge-rate ! "
      "videoscale name=wfd-scale qos=true ! video/x-raw,width=128,height=96,framerate=30/1 ! "
      "videoconvert name=wfd-videoconvert qos=true ! "
      "queue name=wfd-pre-encoder-queue max-size-buffers=1 leaky=no ! "
      "x264enc name=wfd-encoder tune=zerolatency speed-preset=ultrafast key-int-max=30 bframes=0 ! "
      "fakesink sync=false async=false",&error);
  if(!pipeline || error) {
    g_printerr("FAIL: calibration setup: %s\n",error?error->message:"missing pipeline");
    g_clear_error(&error);if(pipeline)gst_object_unref(pipeline);return 1;
  }
  GstElement *source=gst_bin_get_by_name(GST_BIN(pipeline),"panelbridge-input");
  GstElement *rate=gst_bin_get_by_name(GST_BIN(pipeline),"panelbridge-rate");
  GstElement *encoder=gst_bin_get_by_name(GST_BIN(pipeline),"wfd-encoder");
  GstElement *convert=gst_bin_get_by_name(GST_BIN(pipeline),"wfd-videoconvert");
  GstCaps *caps=gst_caps_new_simple("video/x-raw","format",G_TYPE_STRING,"BGRx",
      "width",G_TYPE_INT,64,"height",G_TYPE_INT,48,"framerate",GST_TYPE_FRACTION,fps,1,NULL);
  g_object_set(source,"caps",caps,"is-live",TRUE,"format",GST_FORMAT_TIME,"do-timestamp",TRUE,
      "block",FALSE,"max-buffers",(guint64)1,"max-bytes",(guint64)(64*48*4),"min-latency",(gint64)0,NULL);
  gst_util_set_object_arg(G_OBJECT(source),"leaky-type","downstream");gst_caps_unref(caps);
  pb_wfd_telemetry_calibration_configure(200,600);
  pb_wfd_telemetry_attach_source(source,rate);pb_wfd_telemetry_attach_encoder(encoder);
  g_signal_connect(source,"need-data",G_CALLBACK(need_data),&f);
  if(drop || strip) {
    /* Added after the production sink probe: test observes a real discarded
     * frame or missing downstream metadata, not fabricated counter values. */
    GstPad *pad=gst_element_get_static_pad(convert,"sink");
    gst_pad_add_probe(pad,GST_PAD_PROBE_TYPE_BUFFER,inject,&f,NULL);gst_object_unref(pad);
  }
  pb_wfd_telemetry_calibration_streaming();
  gboolean started=gst_element_set_state(pipeline,GST_STATE_PLAYING)!=GST_STATE_CHANGE_FAILURE;
  GstBus *bus=gst_element_get_bus(pipeline);
  GstMessage *message=started?gst_bus_timed_pop_filtered(bus,3*GST_SECOND,GST_MESSAGE_EOS|GST_MESSAGE_ERROR):NULL;
  gboolean eos=message && GST_MESSAGE_TYPE(message)==GST_MESSAGE_EOS;
  if(message && !eos) { gst_message_parse_error(message,&error,NULL);g_printerr("FAIL: %s\n",error->message);g_clear_error(&error); }
  if(message)gst_message_unref(message);
  /* Bus EOS establishes downstream drain; stop callbacks before reading the
   * fixture-owned flags or freeing their stack storage. */
  gst_element_set_state(pipeline,GST_STATE_NULL);
  gboolean complete=FALSE,closed=pb_wfd_telemetry_calibration_closed(&complete);
  g_print("{\"fixture\":\"native-calibration-v1\",\"fps\":%u,\"burst\":%s,\"drop\":%s,\"strip\":%s,\"zero_epoch\":%s,\"injected\":%s}\n",fps,burst?"true":"false",drop?"true":"false",strip?"true":"false",zero_epoch?"true":"false",f.injected?"true":"false");
  pb_wfd_telemetry_emit(FALSE);
  gboolean good=eos && closed && !f.failed && complete==!strip && (!(drop||strip) || f.injected);
  pb_wfd_telemetry_clear();
  gst_object_unref(bus);gst_object_unref(source);gst_object_unref(rate);gst_object_unref(encoder);gst_object_unref(convert);gst_object_unref(pipeline);
  if(!good)g_printerr("FAIL: calibration fps=%u burst=%d drop=%d strip=%d eos=%d closed=%d complete=%d\n",fps,burst,drop,strip,eos,closed,complete);
  return good?0:1;
}
int main(int argc,char **argv) {
  signal(SIGALRM,watchdog);alarm(25);gst_init(&argc,&argv);
  int failed=run_case(30,FALSE,FALSE,FALSE,FALSE);
  failed|=run_case(15,FALSE,FALSE,FALSE,FALSE);failed|=run_case(60,FALSE,FALSE,FALSE,FALSE);
  failed|=run_case(30,TRUE,FALSE,FALSE,FALSE);failed|=run_case(30,FALSE,TRUE,FALSE,FALSE);
  failed|=run_case(30,FALSE,FALSE,TRUE,FALSE);failed|=run_case(30,FALSE,FALSE,TRUE,TRUE);
  alarm(0);return failed;
}
