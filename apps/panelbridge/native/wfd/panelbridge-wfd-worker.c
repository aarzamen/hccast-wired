/* SPDX-License-Identifier: GPL-3.0-or-later
 * PanelBridge feasibility worker, using GNOME Network Displays 0.99.0.
 * No GTK application, portal, audio capture, or user systemd dependency.
 * The coordinator owns recovery and the helper-owned connection lease.
 */
#include <errno.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <unistd.h>
#include <glib-unix.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <json-glib/json-glib.h>
#include "nd-wfd-p2p-provider.h"
#include "nd-wfd-p2p-sink.h"
#include "wfd/pb-wfd-bridge.h"
#include "wfd/pb-wfd-runtime.h"
#include "wfd/pb-wfd-telemetry.h"

typedef struct {
  GMainLoop *loop;
  NMClient *client;
  NMDevice *device;
  NdWFDP2PProvider *provider;
  NdSink *sink;
  GstElement *test_pipeline;
  gchar *interface_name, *peer_name, *peer_address, *source;
  gchar *connection_uuid, *peer_ip;
  gint width, height, fps, fd, seconds, connect_timeout;
  gint wire_width, wire_height, wire_fps, bitrate_kbps;
  gint calibration_warmup_ms, calibration_duration_ms, calibration_eos;
  gboolean list, self_test, continuous, started, streaming, closing;
  gint stopping, decoded, input_failed;
  gint exit_status;
  guint start_timer, run_timer, choose_timer, drain_timer, telemetry_timer, teardown_timer;
  guint calibration_timer;
  gint64 drain_deadline;
  gint64 calibration_deadline;
} Worker;

static void
event (const char *name, const char *detail, gint64 value)
{
  g_autoptr(JsonBuilder) builder = json_builder_new ();
  g_autoptr(JsonGenerator) generator = json_generator_new ();
  json_builder_begin_object (builder);
  json_builder_set_member_name (builder, "event");
  json_builder_add_string_value (builder, name);
  json_builder_set_member_name (builder, "detail");
  json_builder_add_string_value (builder, detail ? detail : "");
  json_builder_set_member_name (builder, "value");
  json_builder_add_int_value (builder, value);
  json_builder_set_member_name (builder, "monotonic_us");
  json_builder_add_int_value (builder, g_get_monotonic_time ());
  json_builder_end_object (builder);
  JsonNode *root = json_builder_get_root (builder);
  json_generator_set_root (generator, root);
  g_autofree gchar *line = json_generator_to_data (generator, NULL);
  g_print ("%s\n", line);
  json_node_free (root);
}

static void
clear_timer (guint *timer)
{
  if (*timer) {
    g_source_remove (*timer);
    *timer = 0;
  }
}

static gboolean
option_supplied (int argc, char **argv, const char *name)
{
  gsize length = strlen (name);
  for (int i = 1; i < argc; i++)
    if (g_str_equal (argv[i], name) ||
        (g_str_has_prefix (argv[i], name) && argv[i][length] == '=')) return TRUE;
  return FALSE;
}

static gboolean
drain_complete (gpointer data)
{
  Worker *w = data;
  if (w->teardown_timer) return G_SOURCE_CONTINUE;
  if (w->connection_uuid) {
    event ("cleanup", "external-connection-retained-for-helper", 0);
    w->drain_timer = 0;
    g_main_loop_quit (w->loop);
    return G_SOURCE_REMOVE;
  }
  if (!w->device || !nm_device_get_active_connection (w->device)) {
    event ("cleanup", "p2p-inactive", 0);
    w->drain_timer = 0;
    g_main_loop_quit (w->loop);
    return G_SOURCE_REMOVE;
  }
  if (g_get_monotonic_time () >= w->drain_deadline) {
    event ("cleanup", "p2p-still-active-after-deadline", 1);
    if (w->exit_status == 0) w->exit_status = 6;
    w->drain_timer = 0;
    g_main_loop_quit (w->loop);
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;
}

static gboolean
stop_sink_now (gpointer data)
{
  Worker *w = data;
  w->teardown_timer = 0;
  if (w->sink) nd_sink_stop_stream (w->sink);
  event ("teardown", "bounded-receiver-grace-finished", 0);
  return G_SOURCE_REMOVE;
}

static void
finish (Worker *w, gint status, const char *reason)
{
  if (w->closing) return;
  w->closing = TRUE;
  w->exit_status = status;
  g_atomic_int_set (&w->stopping, 1);
  clear_timer (&w->start_timer);
  clear_timer (&w->run_timer);
  clear_timer (&w->choose_timer);
  clear_timer (&w->telemetry_timer);
  clear_timer (&w->calibration_timer);
  if (status != 0) pb_wfd_telemetry_calibration_abort (reason);
  event ("stopping", reason, status);
  if (!w->list) pb_wfd_telemetry_emit (FALSE);
  if (w->provider) g_object_set (w->provider, "discover", FALSE, NULL);
  if (w->sink && ND_IS_WFD_P2P_SINK (w->sink) &&
      nd_wfd_p2p_sink_request_teardown (ND_WFD_P2P_SINK (w->sink))) {
    event ("teardown", "receiver-requested-before-network-release", 0);
    w->teardown_timer = g_timeout_add (1000, stop_sink_now, w);
  } else if (w->sink) nd_sink_stop_stream (w->sink);
  if (w->test_pipeline) gst_element_set_state (w->test_pipeline, GST_STATE_NULL);
  w->drain_deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  w->drain_timer = g_timeout_add (100, drain_complete, w);
}

static gboolean
input_failure (gpointer data)
{
  Worker *w = data;
  finish (w, 5, g_atomic_int_get (&w->input_failed) == 2 ?
      "raw-input-push-failed" : "raw-input-eof-or-read-error");
  return G_SOURCE_REMOVE;
}

/* need-data runs on appsrc's streaming thread. poll bounds stop latency. */
static void
raw_need_data (GstAppSrc *appsrc, guint requested, gpointer data)
{
  Worker *w = data;
  gsize size = (gsize) w->width * w->height * 4;
  gsize offset = 0;
  GstBuffer *buffer = gst_buffer_new_allocate (NULL, size, NULL);
  GstMapInfo map;
  (void) requested;
  if (g_atomic_int_get (&w->calibration_eos)) {
    if (buffer) gst_buffer_unref (buffer);
    return;
  }
  if (!buffer || !gst_buffer_map (buffer, &map, GST_MAP_WRITE)) {
    if (buffer) gst_buffer_unref (buffer);
    if (g_atomic_int_compare_and_exchange (&w->input_failed, 0, 1))
      g_idle_add (input_failure, w);
    return;
  }
  while (offset < size && !g_atomic_int_get (&w->stopping)) {
    struct pollfd pfd = { .fd = w->fd, .events = POLLIN };
    int result = poll (&pfd, 1, 100);
    if (result < 0 && errno == EINTR) continue;
    if (result == 0) continue;
    if (result < 0 || (pfd.revents & (POLLERR | POLLNVAL))) break;
    ssize_t amount = read (w->fd, map.data + offset, size - offset);
    if (amount < 0 && (errno == EINTR || errno == EAGAIN)) continue;
    if (amount <= 0) break;
    offset += (gsize) amount;
  }
  gst_buffer_unmap (buffer, &map);
  if (offset != size || g_atomic_int_get (&w->stopping)) {
    gst_buffer_unref (buffer);
    gst_app_src_end_of_stream (appsrc);
    if (!g_atomic_int_get (&w->stopping) &&
        g_atomic_int_compare_and_exchange (&w->input_failed, 0, 1))
      g_idle_add (input_failure, w);
    return;
  }
  GST_BUFFER_DURATION (buffer) = GST_SECOND / w->fps;
  guint64 sequence = 0;
  if (!pb_wfd_telemetry_raw_begin (buffer, &sequence)) {
    gst_buffer_unref (buffer);
    g_atomic_int_set (&w->calibration_eos, 1);
    gst_app_src_end_of_stream (appsrc);
    return;
  }
  GstFlowReturn flow = gst_app_src_push_buffer (appsrc, buffer);
  pb_wfd_telemetry_raw_result (sequence, flow);
  if (flow == GST_FLOW_OK) pb_wfd_telemetry_raw_pushed ();
  else if (flow == GST_FLOW_FLUSHING) {
    /* RTSP RESET suspension can begin while need-data is reading a frame.
     * Consume that complete frame to retain pipe alignment, then let appsrc
     * restart normally. Startup and progress deadlines still bound a reset
     * that never resumes; other flow failures remain fatal. */
    if (!g_atomic_int_get (&w->stopping))
      event ("raw-input-reset-discarded", "complete-frame-during-flushing", 1);
  }
  else if (!g_atomic_int_get (&w->stopping) &&
           g_atomic_int_compare_and_exchange (&w->input_failed, 0, 2)) {
    event ("raw-input-flow", gst_flow_get_name (flow), flow);
    g_idle_add (input_failure, w);
  }
}

static GstElement *
create_source (NdSink *sink, gpointer data)
{
  Worker *w = data;
  GstElement *bin = gst_bin_new ("panelbridge-source");
  GstElement *src = gst_element_factory_make (
      g_str_equal (w->source, "raw") ? "appsrc" : "videotestsrc", "panelbridge-input");
  GstElement *filter = gst_element_factory_make ("capsfilter", "panelbridge-input-caps");
  GstElement *rate = gst_element_factory_make ("videorate", "panelbridge-rate");
  (void) sink;
  if (!bin || !src || !filter || !rate) g_error ("Required source element unavailable");
  GstCaps *caps = gst_caps_new_simple ("video/x-raw", "format", G_TYPE_STRING, "BGRx",
      "width", G_TYPE_INT, w->width, "height", G_TYPE_INT, w->height,
      "framerate", GST_TYPE_FRACTION, w->fps, 1, NULL);
  if (g_str_equal (w->source, "raw")) {
    g_object_set (src, "caps", caps, "is-live", TRUE, "do-timestamp", TRUE,
        "format", GST_FORMAT_TIME, "min-latency", (gint64) 0,
        "block", FALSE, "max-buffers", (guint64) 1,
        "max-bytes", (guint64) ((gsize) w->width * w->height * 4), NULL);
    gst_util_set_object_arg (G_OBJECT (src), "leaky-type", "downstream");
    g_signal_connect (src, "need-data", G_CALLBACK (raw_need_data), w);
  } else {
    g_object_set (src, "is-live", TRUE, "do-timestamp", TRUE,
        "foreground-color", 0xffffffffu, "background-color", 0xff000000u, NULL);
    gst_util_set_object_arg (G_OBJECT (src), "pattern", "ball");
  }
  g_object_set (filter, "caps", caps, NULL);
  gst_caps_unref (caps);
  gst_bin_add_many (GST_BIN (bin), src, filter, rate, NULL);
  if (!gst_element_link_many (src, filter, rate, NULL)) g_error ("Source link failed");
  GstPad *pad = gst_element_get_static_pad (rate, "src");
  gst_element_add_pad (bin, gst_ghost_pad_new ("src", pad));
  gst_object_unref (pad);
  pb_wfd_telemetry_attach_source (src, rate);
  event ("source-created", w->source, (gint64) w->width * w->height * 4);
  return g_object_ref_sink (bin);
}

static GstElement *
no_audio (NdSink *sink, gpointer data)
{
  (void) sink; (void) data;
  return NULL;
}

static gboolean
run_complete (gpointer data)
{
  Worker *w = data;
  w->run_timer = 0;
  if (w->self_test && g_atomic_int_get (&w->decoded) == 0)
    finish (w, 5, "self-test-produced-no-decoded-frames");
  else
    finish (w, 0, w->self_test ? "source-encode-decode-window-complete" :
        w->list ? "discovery-window-complete" : "stream-window-complete");
  return G_SOURCE_REMOVE;
}

static gboolean
start_expired (gpointer data)
{
  Worker *w = data;
  w->start_timer = 0;
  finish (w, 3, "connection-deadline");
  return G_SOURCE_REMOVE;
}

typedef struct { Worker *worker; NdSinkState state; } StateEvent;

static gboolean
calibration_progress (gpointer data)
{
  Worker *w = data;
  if (g_atomic_int_compare_and_exchange (&w->calibration_eos, 1, 2)) {
    event ("calibration-eos-requested", "complete-source-window; bounded-drain", 0);
    /* The overall bound still wins if the source stalled before its boundary. */
    w->calibration_deadline = MIN (w->calibration_deadline,
        g_get_monotonic_time () + 5 * G_USEC_PER_SEC);
  }
  gboolean complete = FALSE;
  if (pb_wfd_telemetry_calibration_closed (&complete)) {
    w->calibration_timer = 0;
    finish (w, complete ? 0 : 5, complete ? "calibration-cohort-complete" :
        "calibration-cohort-incomplete");
    return G_SOURCE_REMOVE;
  }
  if (g_get_monotonic_time () >= w->calibration_deadline) {
    w->calibration_timer = 0;
    finish (w, 5, "calibration-window-or-drain-deadline");
    return G_SOURCE_REMOVE;
  }
  return G_SOURCE_CONTINUE;
}

static gboolean
handle_state (gpointer data)
{
  StateEvent *change = data;
  Worker *w = change->worker;
  NdSinkState state = change->state;
  g_autofree gchar *name = g_enum_to_string (ND_TYPE_SINK_STATE, state);
  event ("state", name, state);
  if (!w->closing && state == ND_SINK_STATE_STREAMING && !w->streaming) {
    w->streaming = TRUE;
    clear_timer (&w->start_timer);
    if (w->calibration_duration_ms) {
      pb_wfd_telemetry_calibration_streaming ();
      w->calibration_deadline = g_get_monotonic_time () +
          ((gint64) w->calibration_warmup_ms + w->calibration_duration_ms + 5000) * 1000;
      w->calibration_timer = g_timeout_add (50, calibration_progress, w);
    } else {
      guint duration = pb_runtime_seconds (w->continuous, w->seconds);
      if (duration) w->run_timer = g_timeout_add_seconds (duration, run_complete, w);
    }
  } else if (!w->closing && (state == ND_SINK_STATE_ERROR ||
             (state == ND_SINK_STATE_DISCONNECTED && w->started))) {
    gboolean complete = FALSE;
    if (g_atomic_int_get (&w->calibration_eos) &&
        pb_wfd_telemetry_calibration_closed (&complete) && complete)
      finish (w, 0, "calibration-cohort-complete-after-eos");
    else finish (w, 4, "sink-disconnected-or-error");
  }
  return G_SOURCE_REMOVE;
}

static void
state_changed (NdSink *sink, GParamSpec *pspec, gpointer data)
{
  StateEvent *change = g_new0 (StateEvent, 1);
  (void) pspec;
  change->worker = data;
  g_object_get (sink, "state", &change->state, NULL);
  /* RTSP may emit from a streaming thread; serialize controller actions. */
  g_idle_add_full (G_PRIORITY_DEFAULT, handle_state, change, g_free);
}

static gboolean
peer_matches (Worker *w, NdSink *sink)
{
  if (!ND_IS_WFD_P2P_SINK (sink)) return FALSE;
  NMWifiP2PPeer *peer = nd_wfd_p2p_sink_get_peer (ND_WFD_P2P_SINK (sink));
  if (w->peer_name && g_strcmp0 (w->peer_name, nm_wifi_p2p_peer_get_name (peer)) != 0)
    return FALSE;
  if (w->peer_address && g_ascii_strcasecmp (w->peer_address,
        nm_wifi_p2p_peer_get_hw_address (peer) ?: "") != 0) return FALSE;
  return TRUE;
}

static void
sink_added (NdProvider *provider, NdSink *sink, gpointer data)
{
  Worker *w = data;
  g_autofree gchar *name = NULL;
  (void) provider;
  g_object_get (sink, "display-name", &name, NULL);
  event ("discovered", name, peer_matches (w, sink));
}

static gboolean
choose_peer (gpointer data)
{
  Worker *w = data;
  g_autoptr(GList) sinks = nd_provider_get_sinks (ND_PROVIDER (w->provider));
  NdSink *selected = NULL;
  guint count = 0;
  for (GList *item = sinks; item; item = item->next) {
    if (peer_matches (w, item->data)) { selected = item->data; count++; }
  }
  if (count == 0) return G_SOURCE_CONTINUE;
  if (count > 1) {
    w->choose_timer = 0;
    finish (w, 2, "ambiguous-peer-use-address");
    return G_SOURCE_REMOVE;
  }
  w->sink = g_object_ref (selected);
  g_signal_connect (w->sink, "create-source", G_CALLBACK (create_source), w);
  g_signal_connect (w->sink, "create-audio-source", G_CALLBACK (no_audio), w);
  g_signal_connect (w->sink, "notify::state", G_CALLBACK (state_changed), w);
  w->started = TRUE;
  NdSink *started = nd_sink_start_stream (w->sink);
  if (started) g_object_unref (started);
  else finish (w, 4, "start-stream-rejected");
  /* Match the upstream UI: disable discovery after stream startup. */
  g_object_set (w->provider, "discover", FALSE, NULL);
  w->choose_timer = 0;
  return G_SOURCE_REMOVE;
}

static gboolean
adopt_existing (Worker *w)
{
  NMActiveConnection *ac = nm_device_get_active_connection (w->device);
  if (!ac || g_strcmp0 (nm_active_connection_get_uuid (ac), w->connection_uuid) != 0 ||
      nm_active_connection_get_state (ac) != NM_ACTIVE_CONNECTION_STATE_ACTIVATED ||
      g_strcmp0 (nm_active_connection_get_connection_type (ac), "wifi-p2p") != 0) {
    event ("network-error", "active-connection-uuid-type-or-state-mismatch", 2);
    return FALSE;
  }
  const gchar *specific = nm_active_connection_get_specific_object_path (ac);
  NMWifiP2PPeer *peer = specific ? nm_device_wifi_p2p_get_peer_by_path (
      NM_DEVICE_WIFI_P2P (w->device), specific) : NULL;
  if (!peer || g_ascii_strcasecmp (w->peer_address,
      nm_wifi_p2p_peer_get_hw_address (peer) ?: "") != 0 ||
      (w->peer_name && g_strcmp0 (w->peer_name, nm_wifi_p2p_peer_get_name (peer)) != 0)) {
    event ("network-error", "active-connection-peer-identity-mismatch", 2);
    return FALSE;
  }
  GBytes *wfd_ies = nm_wifi_p2p_peer_get_wfd_ies (peer);
  if (!wfd_ies || g_bytes_get_size (wfd_ies) == 0) {
    event ("network-error", "active-peer-has-no-wfd-ies", 2);
    return FALSE;
  }
  w->sink = ND_SINK (nd_wfd_p2p_sink_new (w->client, w->device, peer));
  g_signal_connect (w->sink, "create-source", G_CALLBACK (create_source), w);
  g_signal_connect (w->sink, "create-audio-source", G_CALLBACK (no_audio), w);
  g_signal_connect (w->sink, "notify::state", G_CALLBACK (state_changed), w);
  w->started = TRUE;
  w->start_timer = g_timeout_add_seconds (w->connect_timeout, start_expired, w);
  if (!nd_wfd_p2p_sink_adopt_connection (ND_WFD_P2P_SINK (w->sink), ac, w->peer_ip)) {
    finish (w, 4, "adopt-connection-rejected");
  } else {
    event ("adopted", "verified-connection-and-peer; helper-owns-network", 0);
  }
  return TRUE;
}

static gboolean
signal_stop (gpointer data)
{
  finish (data, 130, "signal");
  return G_SOURCE_CONTINUE;
}

static gboolean
report_pipeline (gpointer data)
{
  (void) data;
  pb_wfd_telemetry_emit (FALSE);
  return G_SOURCE_CONTINUE;
}

static void
decoded_frame (GstElement *sink, GstBuffer *buffer, GstPad *pad, gpointer data)
{
  Worker *w = data;
  (void) sink; (void) buffer; (void) pad;
  g_atomic_int_inc (&w->decoded);
}

static gboolean
test_bus (GstBus *bus, GstMessage *message, gpointer data)
{
  Worker *w = data;
  (void) bus;
  if (GST_MESSAGE_TYPE (message) == GST_MESSAGE_ERROR) {
    GError *error = NULL;
    gchar *debug = NULL;
    gst_message_parse_error (message, &error, &debug);
    event ("pipeline-error", error->message, 0);
    g_clear_error (&error); g_free (debug);
    finish (w, 5, "self-test-pipeline-error");
  } else if (GST_MESSAGE_TYPE (message) == GST_MESSAGE_EOS) {
    finish (w, 5, "unexpected-self-test-eos");
  }
  return G_SOURCE_CONTINUE;
}

int
main (int argc, char **argv)
{
  Worker w = { .width = 1280, .height = 720, .fps = 30, .fd = 0,
      .seconds = 30, .connect_timeout = 45 };
  unsigned wire_fields = (option_supplied (argc, argv, "--wire-width") ? 1 : 0) |
      (option_supplied (argc, argv, "--wire-height") ? 2 : 0) |
      (option_supplied (argc, argv, "--wire-fps") ? 4 : 0);
  gboolean bitrate_set = option_supplied (argc, argv, "--bitrate-kbps");
  gboolean seconds_explicit = option_supplied (argc, argv, "--seconds");
  gboolean calibration_warmup_set = option_supplied (argc, argv, "--calibration-warmup-ms");
  gboolean calibration_duration_set = option_supplied (argc, argv, "--calibration-duration-ms");
  GOptionEntry options[] = {
    {"interface", 0, 0, G_OPTION_ARG_STRING, &w.interface_name, "Exact NM P2P interface", "IFACE"},
    {"peer-name", 0, 0, G_OPTION_ARG_STRING, &w.peer_name, "Exact owned receiver name", "NAME"},
    {"peer-address", 0, 0, G_OPTION_ARG_STRING, &w.peer_address, "Exact owned peer address", "ADDRESS"},
    {"connection-uuid", 0, 0, G_OPTION_ARG_STRING, &w.connection_uuid, "Adopt exact helper-owned active connection", "UUID"},
    {"peer-ip", 0, 0, G_OPTION_ARG_STRING, &w.peer_ip, "Helper-verified receiver IPv4 for RTSP admission", "IPV4"},
    {"source", 0, 0, G_OPTION_ARG_STRING, &w.source, "test or raw (BGRx)", "SOURCE"},
    {"width", 0, 0, G_OPTION_ARG_INT, &w.width, "Raw/source width", "PIXELS"},
    {"height", 0, 0, G_OPTION_ARG_INT, &w.height, "Raw/source height", "PIXELS"},
    {"fps", 0, 0, G_OPTION_ARG_INT, &w.fps, "Content/source rate", "FPS"},
    {"wire-width", 0, 0, G_OPTION_ARG_INT, &w.wire_width, "Requested advertised wire width (all wire fields required)", "PIXELS"},
    {"wire-height", 0, 0, G_OPTION_ARG_INT, &w.wire_height, "Requested advertised wire height", "PIXELS"},
    {"wire-fps", 0, 0, G_OPTION_ARG_INT, &w.wire_fps, "Requested wire refresh (30)", "FPS"},
    {"bitrate-kbps", 0, 0, G_OPTION_ARG_INT, &w.bitrate_kbps, "Explicit encoder target, 512..8000 kbit/s", "KBPS"},
    {"fd", 0, 0, G_OPTION_ARG_INT, &w.fd, "Inherited raw frame input FD", "FD"},
    {"seconds", 0, 0, G_OPTION_ARG_INT, &w.seconds, "Streaming or discovery duration", "SECONDS"},
    {"continuous", 0, 0, G_OPTION_ARG_NONE, &w.continuous, "Adopted raw session until signal, input loss or RTSP loss; excludes --seconds", NULL},
    {"calibration-warmup-ms", 0, 0, G_OPTION_ARG_INT, &w.calibration_warmup_ms, "Optional raw cohort warmup, 5000..60000 ms; requires duration", "MS"},
    {"calibration-duration-ms", 0, 0, G_OPTION_ARG_INT, &w.calibration_duration_ms, "Optional raw cohort window, at least 30000 ms; then bounded EOS drain", "MS"},
    {"connect-timeout", 0, 0, G_OPTION_ARG_INT, &w.connect_timeout, "Setup deadline", "SECONDS"},
    {"list", 0, 0, G_OPTION_ARG_NONE, &w.list, "Bounded discovery only", NULL},
    {"self-test-source", 0, 0, G_OPTION_ARG_NONE, &w.self_test, "Encode/decode source without NM", NULL},
    {NULL}
  };
  g_autoptr(GOptionContext) context = g_option_context_new ("- PanelBridge WFD worker");
  g_autoptr(GError) error = NULL;
  g_option_context_add_main_entries (context, options, NULL);
  g_option_context_add_group (context, gst_init_get_option_group ());
  if (!g_option_context_parse (context, &argc, &argv, &error)) {
    event ("argument-error", error->message, 2); return 2;
  }
  if (!w.source) w.source = g_strdup ("test");
  const char *runtime_rejection = pb_runtime_rejection (w.continuous, seconds_explicit,
      w.connection_uuid != NULL, g_str_equal (w.source, "raw"), w.list, w.self_test);
  if (runtime_rejection) {
    event ("argument-error", runtime_rejection, 2); return 2;
  }
  const char *calibration_rejection = pb_calibration_rejection (
      calibration_warmup_set, calibration_duration_set,
      w.calibration_warmup_ms, w.calibration_duration_ms,
      g_str_equal (w.source, "raw"), w.connection_uuid != NULL,
      w.continuous, seconds_explicit, w.list, w.self_test);
  if (calibration_rejection) {
    event ("argument-error", calibration_rejection, 2); return 2;
  }
  PbWfdRequest profile_request = {wire_fields, w.wire_width, w.wire_height, w.wire_fps,
      bitrate_set, w.bitrate_kbps};
  PbWfdProfileResult profile_valid = pb_wfd_validate_request (&profile_request);
  if (profile_valid != PB_WFD_PROFILE_OK) {
    event ("argument-error", pb_wfd_profile_result_name (profile_valid), 2); return 2;
  }
  if ((w.list || w.self_test) && (wire_fields || bitrate_set)) {
    event ("argument-error", "wire-profile-requires-a-receiver-session", 2); return 2;
  }
  pb_wfd_configure_request (&profile_request, w.width, w.height, w.fps);
  if (argc != 1 || w.width < 16 || w.width > 1920 || w.height < 16 || w.height > 1080 ||
      w.width % 2 || w.height % 2 || w.fps < 1 || w.fps > 60 || w.fd < 0 || w.fd > 1024 ||
      w.seconds < 1 || w.seconds > 14400 || w.connect_timeout < 1 || w.connect_timeout > 120 ||
      !(g_str_equal (w.source, "test") || g_str_equal (w.source, "raw")) ||
      (w.list && w.self_test) || (!w.self_test && !w.interface_name) ||
      (w.connection_uuid && (w.list || w.self_test || !w.peer_address || !w.peer_ip)) ||
      (w.peer_ip && !w.connection_uuid) ||
      (!w.list && !w.self_test && !w.peer_name && !w.peer_address)) {
    event ("argument-error", "invalid bounds or missing exact interface/peer", 2); return 2;
  }
  if (w.peer_ip) {
    g_autoptr(GInetAddress) address = g_inet_address_new_from_string (w.peer_ip);
    if (!address || g_inet_address_get_family (address) != G_SOCKET_FAMILY_IPV4 ||
        !g_uuid_string_is_valid (w.connection_uuid)) {
      event ("argument-error", "invalid-adoption-uuid-or-ipv4", 2); return 2;
    }
  }
  w.loop = g_main_loop_new (NULL, FALSE);
  if (calibration_duration_set)
    pb_wfd_telemetry_calibration_configure (w.calibration_warmup_ms, w.calibration_duration_ms);
  guint sigint = g_unix_signal_add (SIGINT, signal_stop, &w);
  guint sigterm = g_unix_signal_add (SIGTERM, signal_stop, &w);
  guint bus_watch = 0;
  event ("starting", w.self_test ? "self-test" : w.list ? "discovery" : w.source,
      pb_runtime_seconds (w.continuous, w.seconds));
  if (w.continuous) event ("run-mode", "continuous-adopted-raw", 0);
  if (calibration_duration_set) event ("run-mode", "bounded-calibration-adopted-raw", w.calibration_duration_ms);
  if (!w.list) w.telemetry_timer = g_timeout_add (1000, report_pipeline, &w);
  if (w.self_test) {
    w.test_pipeline = gst_parse_launch (
        "videoconvert name=convert ! video/x-raw,format=I420 ! "
        "queue name=wfd-pre-encoder-queue max-size-buffers=1 leaky=no ! "
        "x264enc name=wfd-encoder tune=zerolatency speed-preset=ultrafast pass=cbr bitrate=4096 key-int-max=30 ! "
        "h264parse ! avdec_h264 ! fakesink name=decoded sync=true signal-handoffs=true", &error);
    if (!w.test_pipeline || error) {
      event ("pipeline-error", error ? error->message : "parse failed", 5); return 5;
    }
    GstElement *source = create_source (NULL, &w);
    GstElement *convert = gst_bin_get_by_name (GST_BIN (w.test_pipeline), "convert");
    GstElement *decoded = gst_bin_get_by_name (GST_BIN (w.test_pipeline), "decoded");
    GstElement *encoder = gst_bin_get_by_name (GST_BIN (w.test_pipeline), "wfd-encoder");
    pb_wfd_telemetry_attach_encoder (encoder);
    gst_object_unref (encoder);
    gst_bin_add (GST_BIN (w.test_pipeline), source);
    if (!gst_element_link (source, convert)) { event ("pipeline-error", "link failed", 5); return 5; }
    gst_object_unref (source);
    g_signal_connect (decoded, "handoff", G_CALLBACK (decoded_frame), &w);
    gst_object_unref (decoded); gst_object_unref (convert);
    GstBus *bus = gst_element_get_bus (w.test_pipeline);
    bus_watch = gst_bus_add_watch (bus, test_bus, &w);
    gst_object_unref (bus);
    if (gst_element_set_state (w.test_pipeline, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
      event ("pipeline-error", "PLAYING failed", 5); return 5;
    }
    w.run_timer = g_timeout_add_seconds (w.seconds, run_complete, &w);
  } else {
    w.client = nm_client_new (NULL, &error);
    if (!w.client) { event ("network-error", error->message, 2); return 2; }
    w.device = nm_client_get_device_by_iface (w.client, w.interface_name);
    if (!w.device || !NM_IS_DEVICE_WIFI_P2P (w.device)) {
      event ("network-error", "exact-P2P-interface-not-found", 2); return 2;
    }
    if (!w.connection_uuid && nm_device_get_active_connection (w.device)) {
      event ("network-error", "P2P-interface-already-active", 2); return 2;
    }
    if (w.connection_uuid) {
      if (!adopt_existing (&w)) return 2;
    } else {
    w.provider = nd_wfd_p2p_provider_new (w.client, w.device);
    g_signal_connect (w.provider, "sink-added", G_CALLBACK (sink_added), &w);
    g_autoptr(GList) initial = nd_provider_get_sinks (ND_PROVIDER (w.provider));
    for (GList *item = initial; item; item = item->next)
      sink_added (ND_PROVIDER (w.provider), item->data, &w);
    if (w.list) w.run_timer = g_timeout_add_seconds (w.seconds, run_complete, &w);
    else {
      w.start_timer = g_timeout_add_seconds (w.connect_timeout, start_expired, &w);
      w.choose_timer = g_timeout_add_seconds (2, choose_peer, &w);
    }
    }
  }
  g_main_loop_run (w.loop);
  g_atomic_int_set (&w.stopping, 1);
  clear_timer (&w.telemetry_timer);
  clear_timer (&w.calibration_timer);
  clear_timer (&w.teardown_timer);
  if (bus_watch) g_source_remove (bus_watch);
  if (w.test_pipeline) { gst_element_set_state (w.test_pipeline, GST_STATE_NULL); gst_object_unref (w.test_pipeline); }
  g_clear_object (&w.sink); g_clear_object (&w.provider); g_clear_object (&w.client);
  g_source_remove (sigint); g_source_remove (sigterm);
  if (!w.list) pb_wfd_telemetry_emit (TRUE);
  event ("raw-frames-pushed", "source-frames-only", pb_wfd_telemetry_raw_frames ());
  event ("self-test-decoded-frames", "software-only", g_atomic_int_get (&w.decoded));
  event ("finished", "physical-output-unverified", w.exit_status);
  pb_wfd_telemetry_clear ();
  g_main_loop_unref (w.loop);
  g_free (w.interface_name); g_free (w.peer_name); g_free (w.peer_address); g_free (w.source);
  g_free (w.connection_uuid); g_free (w.peer_ip);
  return w.exit_status;
}
