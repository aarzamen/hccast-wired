/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdio.h>
#include <string.h>
#include <gst/gst.h>
#include "wfd/pb-wfd-bridge.h"
#include "wfd/wfd-media-factory.h"
#define CHECK(expr) do { if (!(expr)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #expr); return 1; } } while (0)

static WfdParams *
advertise (guint32 cea_mask)
{
  WfdParams *params = wfd_params_new ();
  g_autofree gchar *body = g_strdup_printf (
      "wfd_video_formats: 00 00 01 01 %08x 00000000 00000000 00 0000 0000 00 none none\r\n",
      cea_mask);
  wfd_params_from_sink (params, (const guint8 *) body, strlen (body));
  return params;
}

int main (int argc, char **argv)
{
  gst_init (&argc, &argv);
  PbWfdRequest request = { .wire_fields=7, .width=1280, .height=720, .fps=30,
      .bitrate_set=true, .bitrate_kbps=2000 };
  pb_wfd_configure_request (&request,1280,720,15);
  {
    g_autoptr(WfdParams) defaults = wfd_params_new ();
    CHECK(!pb_wfd_select_requested_mode(defaults,1));
  }
  {
    g_autoptr(WfdParams) advertised = advertise (1u<<5); /* CEA 720p30 */
    CHECK(pb_wfd_select_requested_mode(advertised,1));
    CHECK(advertised->selected_resolution->width == 1280);
    CHECK(advertised->selected_resolution->height == 720);
    CHECK(advertised->selected_resolution->refresh_rate == 30);
    CHECK(!advertised->selected_resolution->interlaced);
    CHECK(pb_wfd_validate_selected_bitrate(advertised));
  }
  {
    g_autoptr(WfdParams) wrong_mode = advertise (1u<<7); /* CEA 1080p30 */
    CHECK(!pb_wfd_select_requested_mode(wrong_mode,1));
  }
  request.width=1920; request.height=1080;
  pb_wfd_configure_request (&request,1280,720,15);
  {
    g_autoptr(WfdParams) interlaced = advertise (1u<<9); /* CEA 1080i60 */
    CHECK(!pb_wfd_select_requested_mode(interlaced,1));
  }
  {
    g_autoptr(WfdParams) wrong_refresh = advertise (1u<<8); /* CEA 1080p60 */
    CHECK(!pb_wfd_select_requested_mode(wrong_refresh,1));
  }
  g_autoptr(WfdParams) params = advertise ((1u<<5)|(1u<<7));
  CHECK(pb_wfd_select_requested_mode(params,1));
  CHECK(params->selected_resolution->width == 1920 && params->selected_resolution->height == 1080);

  /* Exercise the real GND property setter without a radio or RTSP connection. */
  GstElement *bin = gst_bin_new ("profile-test");
  GstElement *encoder = gst_element_factory_make ("x264enc", "wfd-encoder");
  GstElement *sinkfilter = gst_element_factory_make ("capsfilter", "wfd-sinkfilter");
  GstElement *codecfilter = gst_element_factory_make ("capsfilter", "wfd-codecfilter");
  CHECK(bin && encoder && sinkfilter && codecfilter);
  g_object_set_data (G_OBJECT (encoder), "wfd-encoder-impl", GINT_TO_POINTER(ELEMENT_X264));
  gst_bin_add_many (GST_BIN(bin),encoder,sinkfilter,codecfilter,NULL);
  wfd_configure_media_element (GST_BIN(bin),params);
  gint pass = -1;
  guint bitrate = 0;
  g_object_get (encoder,"pass",&pass,"bitrate",&bitrate,NULL);
  CHECK(pass == 0 && bitrate == 2000); /* Actual enum/property values. */
  request.bitrate_set=false;
  pb_wfd_configure_request (&request,1280,720,15);
  wfd_configure_media_element (GST_BIN(bin),params);
  g_object_get (encoder,"pass",&pass,"bitrate",&bitrate,NULL);
  CHECK(pass == 4 && bitrate == 4096); /* Omitted bitrate preserves baseline. */
  gst_object_unref (bin);
  puts("PASS: parsed synthetic receiver advertisements and real encoder properties");
  return 0;
}
