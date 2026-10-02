/* SPDX-License-Identifier: GPL-3.0-or-later
 * Actual worker callback + real GStreamer state reset, without NM or a receiver.
 * The wrappers observe real read/push results; neither simulates a flow return.
 */
#include <fcntl.h>
#include <unistd.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>

static ssize_t fixture_read (int fd, void *buffer, size_t count);
static GstFlowReturn fixture_push (GstAppSrc *source, GstBuffer *buffer);

#define read fixture_read
#define gst_app_src_push_buffer fixture_push
#define main panelbridge_worker_main
#include "panelbridge-wfd-worker.c"
#undef main
#undef gst_app_src_push_buffer
#undef read

typedef struct {
  GMutex lock;
  GCond changed;
  Worker *worker;
  GstAppSrc *appsrc;
  gsize read_bytes;
  guint flushing_returns;
  guint other_bad_returns;
  guint downstream_frames;
  guint malformed_frames;
  gint phase;
} Fixture;

typedef struct {
  GMutex lock;
  GCond changed;
  GstElement *pipeline;
  GstState target;
  GstStateChangeReturn result;
  gboolean done;
  GThread *thread;
} Transition;

static gpointer active_fixture;

static void
watchdog (int signal_number)
{
  static const char message[] = "FAIL: source-reset fixture exceeded 20-second watchdog\n";
  (void) signal_number;
  (void) write (STDERR_FILENO, message, sizeof message - 1);
  _exit (124);
}

static ssize_t
fixture_read (int fd, void *buffer, size_t count)
{
  ssize_t amount = read (fd, buffer, count);
  Fixture *fixture = g_atomic_pointer_get (&active_fixture);
  if (fixture && fd == fixture->worker->fd && amount > 0) {
    g_mutex_lock (&fixture->lock);
    fixture->read_bytes += (gsize) amount;
    g_cond_broadcast (&fixture->changed);
    g_mutex_unlock (&fixture->lock);
  }
  return amount;
}

static GstFlowReturn
fixture_push (GstAppSrc *source, GstBuffer *buffer)
{
  GstFlowReturn flow = gst_app_src_push_buffer (source, buffer);
  Fixture *fixture = g_atomic_pointer_get (&active_fixture);
  if (fixture && source == fixture->appsrc &&
      !g_atomic_int_get (&fixture->worker->stopping)) {
    g_mutex_lock (&fixture->lock);
    if (flow == GST_FLOW_FLUSHING)
      fixture->flushing_returns++;
    else if (flow != GST_FLOW_OK)
      fixture->other_bad_returns++;
    g_cond_broadcast (&fixture->changed);
    g_mutex_unlock (&fixture->lock);
    if (flow != GST_FLOW_OK)
      g_print ("OBSERVED source=%d wire=30 flow=%s code=%d\n",
               fixture->worker->fps, gst_flow_get_name (flow), (int) flow);
  }
  return flow;
}

static gpointer
transition_thread (gpointer data)
{
  Transition *transition = data;
  GstStateChangeReturn result = gst_element_set_state (transition->pipeline, transition->target);
  g_mutex_lock (&transition->lock);
  transition->result = result;
  transition->done = TRUE;
  g_cond_broadcast (&transition->changed);
  g_mutex_unlock (&transition->lock);
  return NULL;
}

static void
transition_start (Transition *transition, GstElement *pipeline, GstState target)
{
  memset (transition, 0, sizeof *transition);
  g_mutex_init (&transition->lock);
  g_cond_init (&transition->changed);
  transition->pipeline = pipeline;
  transition->target = target;
  transition->thread = g_thread_new ("source-reset-state", transition_thread, transition);
}

static gboolean
transition_join (Transition *transition)
{
  gint64 deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  g_mutex_lock (&transition->lock);
  while (!transition->done) {
    if (!g_cond_wait_until (&transition->changed, &transition->lock, deadline)) {
      g_mutex_unlock (&transition->lock);
      /* Do not free objects under an unresolved GStreamer thread or block in
       * an unbounded join. Process exit closes only this fixture's resources. */
      g_printerr ("FAIL: state transition exceeded its 3-second deadline\n");
      _exit (124);
    }
  }
  GstStateChangeReturn result = transition->result;
  g_mutex_unlock (&transition->lock);
  g_thread_join (transition->thread); /* Completion was observed above. */
  transition->thread = NULL;
  g_cond_clear (&transition->changed);
  g_mutex_clear (&transition->lock);
  return result != GST_STATE_CHANGE_FAILURE;
}

static gboolean
bounded_state (GstElement *pipeline, GstState state)
{
  Transition transition;
  transition_start (&transition, pipeline, state);
  return transition_join (&transition);
}

static gboolean
write_bytes (int fd, const guint8 *bytes, gsize count)
{
  gint64 deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  gsize offset = 0;
  while (offset < count && g_get_monotonic_time () < deadline) {
    ssize_t amount = write (fd, bytes + offset, count - offset);
    if (amount > 0) {
      offset += (gsize) amount;
      continue;
    }
    if (amount < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR)
      return FALSE;
    struct pollfd pfd = { .fd = fd, .events = POLLOUT };
    int result = poll (&pfd, 1, 10);
    if (result < 0 && errno != EINTR) return FALSE;
    if (result > 0 && (pfd.revents & (POLLERR | POLLHUP | POLLNVAL))) return FALSE;
  }
  return offset == count;
}

static gboolean
wait_read_bytes (Fixture *fixture, gsize count)
{
  gint64 deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  g_mutex_lock (&fixture->lock);
  while (fixture->read_bytes < count) {
    if (!g_cond_wait_until (&fixture->changed, &fixture->lock, deadline)) break;
  }
  gboolean reached = fixture->read_bytes == count;
  g_mutex_unlock (&fixture->lock);
  return reached;
}

static gboolean
wait_flushing (GstPad *pad)
{
  gint64 deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  while (g_get_monotonic_time () < deadline) {
    GST_OBJECT_LOCK (pad);
    gboolean flushing = GST_PAD_IS_FLUSHING (pad);
    GST_OBJECT_UNLOCK (pad);
    if (flushing) return TRUE;
    g_usleep (1000);
  }
  return FALSE;
}

static void
received_frame (GstElement *sink, GstBuffer *buffer, GstPad *pad, gpointer data)
{
  Fixture *fixture = data;
  GstMapInfo map;
  gboolean valid = FALSE;
  (void) sink;
  (void) pad;
  if (gst_buffer_map (buffer, &map, GST_MAP_READ)) {
    valid = map.size == (gsize) fixture->worker->width * fixture->worker->height * 4;
    if (valid) {
      guint8 marker = map.data[0];
      valid = marker >= 0x61 && marker <= 0x68;
      for (gsize i = 1; valid && i < map.size; i++) valid = map.data[i] == marker;
    }
    gst_buffer_unmap (buffer, &map);
  }
  g_mutex_lock (&fixture->lock);
  if (fixture->phase == 2) {
    fixture->downstream_frames++;
    if (!valid) fixture->malformed_frames++;
    g_cond_broadcast (&fixture->changed);
  }
  g_mutex_unlock (&fixture->lock);
}

static gboolean
wait_downstream (Fixture *fixture)
{
  gint64 deadline = g_get_monotonic_time () + 3 * G_USEC_PER_SEC;
  g_mutex_lock (&fixture->lock);
  while (fixture->downstream_frames < 2) {
    if (!g_cond_wait_until (&fixture->changed, &fixture->lock, deadline)) break;
  }
  gboolean resumed = fixture->downstream_frames >= 2 && fixture->malformed_frames == 0;
  g_mutex_unlock (&fixture->lock);
  return resumed;
}

#define REQUIRE(condition, explanation) do { \
  if (!(condition)) { \
    g_printerr ("FAIL source=%d wire=30: %s\n", fps, explanation); \
    goto cleanup; \
  } \
} while (0)

static int
run_case (gint fps)
{
  int status = 1;
  int pipes[2] = { -1, -1 };
  Worker worker = { .width = 64, .height = 48, .fps = fps, .source = "raw" };
  Fixture fixture = { .worker = &worker };
  Transition reset = {0};
  GstElement *pipeline = NULL, *source = NULL, *appsrc = NULL, *sink = NULL;
  GstPad *source_pad = NULL;
  GError *error = NULL;
  guint8 frame[64 * 48 * 4];
  guint64 pushed_before = pb_wfd_telemetry_raw_frames ();
  const gsize prefix = 4093; /* Deliberately not a pixel or pipe-buffer boundary. */
  g_mutex_init (&fixture.lock);
  g_cond_init (&fixture.changed);
  REQUIRE (pipe (pipes) == 0, "pipe creation");
  REQUIRE (fcntl (pipes[0], F_SETFL, O_NONBLOCK) != -1 &&
           fcntl (pipes[1], F_SETFL, O_NONBLOCK) != -1, "nonblocking pipe setup");
  worker.fd = pipes[0];
  pipeline = gst_parse_launch (
      "capsfilter name=wire caps=video/x-raw,format=BGRx,framerate=30/1 ! "
      "fakesink name=received sync=false async=false signal-handoffs=true", &error);
  REQUIRE (pipeline && !error, "software pipeline creation");
  source = create_source (NULL, &worker); /* Registers the actual raw_need_data. */
  appsrc = gst_bin_get_by_name (GST_BIN (source), "panelbridge-input");
  sink = gst_bin_get_by_name (GST_BIN (pipeline), "received");
  GstElement *wire = gst_bin_get_by_name (GST_BIN (pipeline), "wire");
  gboolean linked = wire && gst_bin_add (GST_BIN (pipeline), source) && gst_element_link (source, wire);
  gst_clear_object (&wire);
  REQUIRE (linked && appsrc && sink, "source/pipeline link");
  fixture.appsrc = GST_APP_SRC (appsrc);
  source_pad = gst_element_get_static_pad (appsrc, "src");
  REQUIRE (source_pad, "appsrc source pad");
  g_signal_connect (sink, "handoff", G_CALLBACK (received_frame), &fixture);
  g_atomic_pointer_set (&active_fixture, &fixture);
  REQUIRE (bounded_state (pipeline, GST_STATE_PLAYING), "initial PLAYING");

  memset (frame, 0x35, sizeof frame);
  REQUIRE (write_bytes (pipes[1], frame, prefix), "partial-frame write");
  REQUIRE (wait_read_bytes (&fixture, prefix), "actual callback consumed partial frame");
  REQUIRE (pb_wfd_telemetry_raw_frames () == pushed_before, "partial frame must not be pushed");
  transition_start (&reset, pipeline, GST_STATE_READY);
  REQUIRE (wait_flushing (source_pad), "reset made actual appsrc source pad flushing");
  REQUIRE (write_bytes (pipes[1], frame + prefix, sizeof frame - prefix),
           "complete the same frame while flushing");
  REQUIRE (transition_join (&reset), "READY reset completed");
  REQUIRE (fixture.flushing_returns == 1 && fixture.other_bad_returns == 0,
           "actual push returned GST_FLOW_FLUSHING exactly once");
  REQUIRE (fixture.read_bytes == sizeof frame, "reset consumed exactly one complete frame");
  g_print ("RESET source=%d wire=30 input_failed=%d\n", fps,
           g_atomic_int_get (&worker.input_failed));

  /* Do not dispatch the worker's fatal idle in the default GLib context. Its
   * input_failed flag is the regression assertion; preserve it across restart. */
  g_mutex_lock (&fixture.lock);
  fixture.phase = 2;
  g_mutex_unlock (&fixture.lock);
  REQUIRE (bounded_state (pipeline, GST_STATE_PLAYING), "recovery PLAYING");
  for (guint i = 0; i < 8; i++) {
    memset (frame, 0x61 + i, sizeof frame);
    REQUIRE (write_bytes (pipes[1], frame, sizeof frame), "recovery frame write");
    REQUIRE (wait_read_bytes (&fixture, (i + 2) * sizeof frame), "recovery frame consumed");
    g_usleep (G_USEC_PER_SEC / fps);
  }
  REQUIRE (wait_downstream (&fixture), "aligned full frames resumed downstream");
  GstBus *bus = gst_element_get_bus (pipeline);
  GstMessage *bus_error = gst_bus_pop_filtered (bus, GST_MESSAGE_ERROR);
  gboolean had_error = bus_error != NULL;
  if (bus_error) {
    GError *detail = NULL;
    gst_message_parse_error (bus_error, &detail, NULL);
    g_printerr ("FAIL source=%d wire=30: GStreamer error: %s\n", fps, detail->message);
    g_clear_error (&detail);
    gst_message_unref (bus_error);
  }
  gst_object_unref (bus);
  REQUIRE (!had_error, "pipeline posted a GStreamer error");
  REQUIRE (g_atomic_int_get (&worker.input_failed) == 0,
           "normal reset incorrectly latched a fatal worker input failure");
  status = 0;

cleanup:
  g_atomic_int_set (&worker.stopping, 1);
  if (reset.thread) (void) transition_join (&reset);
  if (pipeline && !bounded_state (pipeline, GST_STATE_NULL)) status = 1;
  /* Callback threads have stopped before their stack-owned user data expires. */
  if (g_atomic_int_get (&worker.input_failed)) g_source_remove_by_user_data (&worker);
  g_atomic_pointer_set (&active_fixture, NULL);
  pb_wfd_telemetry_clear ();
  gst_clear_object (&source_pad);
  gst_clear_object (&appsrc);
  gst_clear_object (&sink);
  gst_clear_object (&source);
  gst_clear_object (&pipeline);
  g_clear_error (&error);
  if (pipes[0] >= 0) close (pipes[0]);
  if (pipes[1] >= 0) close (pipes[1]);
  g_cond_clear (&fixture.changed);
  g_mutex_clear (&fixture.lock);
  if (status == 0) g_print ("PASS source=%d wire=30: flushing reset, aligned recovery, clean stop\n", fps);
  return status;
}

int
main (int argc, char **argv)
{
  signal (SIGALRM, watchdog);
  signal (SIGPIPE, SIG_IGN);
  alarm (20);
  gst_init (&argc, &argv);
  int first = run_case (15);
  int second = run_case (30);
  alarm (0);
  return first || second;
}
