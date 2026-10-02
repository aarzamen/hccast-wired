/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <json-glib/json-glib.h>
#include "pb-wfd-bridge.h"
#include "pb-wfd-telemetry.h"

/* One immutable request per worker process; initialized before GStreamer threads. */
static PbWfdRequest request;
static gint source_width, source_height, source_fps;

void
pb_wfd_configure_request (const PbWfdRequest *value, gint width, gint height, gint fps)
{
  request = *value;
  source_width = width; source_height = height; source_fps = fps;
}

gboolean pb_wfd_has_wire_request (void) { return request.wire_fields != 0; }
guint pb_wfd_requested_bitrate (void) { return request.bitrate_set ? request.bitrate_kbps : 0; }

static void
add_int (JsonBuilder *builder, const gchar *name, gint64 value)
{
  json_builder_set_member_name (builder, name);
  json_builder_add_int_value (builder, value);
}

static void
add_bool (JsonBuilder *builder, const gchar *name, gboolean value)
{
  json_builder_set_member_name (builder, name);
  json_builder_add_boolean_value (builder, value);
}

static void
add_string (JsonBuilder *builder, const gchar *name, const gchar *value)
{
  json_builder_set_member_name (builder, name);
  json_builder_add_string_value (builder, value ? value : "");
}

static JsonBuilder *
new_record (const gchar *name)
{
  JsonBuilder *builder = json_builder_new ();
  json_builder_begin_object (builder);
  add_string (builder, "event", name);
  add_int (builder, "monotonic_us", g_get_monotonic_time ());
  add_int (builder, "source_width", source_width);
  add_int (builder, "source_height", source_height);
  add_int (builder, "source_fps", source_fps);
  add_bool (builder, "wire_mode_requested", request.wire_fields != 0);
  add_bool (builder, "bitrate_requested", request.bitrate_set);
  return builder;
}

static void
print_record (JsonBuilder *builder)
{
  g_autoptr(JsonGenerator) generator = json_generator_new ();
  json_builder_end_object (builder);
  JsonNode *root = json_builder_get_root (builder);
  json_generator_set_root (generator, root);
  g_autofree gchar *line = json_generator_to_data (generator, NULL);
  g_print ("%s\n", line);
  json_node_free (root);
}

static gboolean
reject_profile (const gchar *reason)
{
  g_autoptr(JsonBuilder) builder = new_record ("profile-rejected");
  add_string (builder, "reason", reason);
  print_record (builder);
  return FALSE;
}

static void
add_wire (JsonBuilder *builder, WfdParams *params)
{
  add_int (builder, "wire_width", params->selected_resolution->width);
  add_int (builder, "wire_height", params->selected_resolution->height);
  add_int (builder, "wire_fps", params->selected_resolution->refresh_rate);
  add_bool (builder, "wire_interlaced", params->selected_resolution->interlaced);
  add_int (builder, "h264_profile", params->selected_codec->profile);
  add_int (builder, "h264_level", params->selected_codec->level);
  add_bool (builder, "receiver_advertisement_received", params->video_formats_received);
  add_bool (builder, "requested_mode_matched_advertisement", request.wire_fields != 0);
}

gboolean
pb_wfd_select_requested_mode (WfdParams *params, guint preferred_profile)
{
  if (!params->video_formats_received)
    return reject_profile ("receiver-video-formats-missing; defaults-are-not-advertisements");
  g_autoptr(GArray) modes = g_array_new (FALSE, FALSE, sizeof (PbWfdAdvertisedMode));
  for (guint i = 0; i < params->video_codecs->len; i++) {
    WfdVideoCodec *codec = g_ptr_array_index (params->video_codecs, i);
    g_autoptr(GList) resolutions = wfd_video_codec_get_resolutions (codec);
    for (GList *item = resolutions; item; item = item->next) {
      WfdResolution *resolution = item->data;
      PbWfdAdvertisedMode mode = {resolution->width, resolution->height,
          resolution->refresh_rate, resolution->interlaced, codec->profile, codec->level, i};
      g_array_append_val (modes, mode);
    }
  }
  size_t selected_index = 0;
  unsigned selected_profile = 0;
  PbWfdProfileResult result = pb_wfd_select_advertised_mode (&request,
      (const PbWfdAdvertisedMode *) modes->data, modes->len, preferred_profile,
      &selected_index, &selected_profile);
  if (result != PB_WFD_PROFILE_OK)
    return reject_profile (pb_wfd_profile_result_name (result));
  PbWfdAdvertisedMode selected = g_array_index (modes, PbWfdAdvertisedMode, selected_index);
  WfdVideoCodec *advertised = g_ptr_array_index (params->video_codecs, selected.codec_index);
  g_clear_pointer (&params->selected_codec, wfd_video_codec_unref);
  g_clear_pointer (&params->selected_resolution, wfd_resolution_free);
  /* Preserve all advertised slicing/format fields and narrow profile bitmaps. */
  WfdVideoCodec *codec = wfd_video_codec_new ();
  *codec = *advertised;
  codec->ref_count = 1;
  codec->native = advertised->native ? wfd_resolution_copy (advertised->native) : NULL;
  codec->profile = selected_profile;
  params->selected_codec = codec;
  params->selected_resolution = wfd_resolution_new ();
  *params->selected_resolution = (WfdResolution) {selected.width, selected.height, selected.fps, FALSE};
  g_autoptr(JsonBuilder) builder = new_record ("wire-mode-selected");
  add_wire (builder, params);
  print_record (builder);
  return TRUE;
}

gboolean
pb_wfd_validate_selected_bitrate (WfdParams *params)
{
  if (!request.bitrate_set) return TRUE;
  if (!params->selected_codec ||
      (guint) request.bitrate_kbps > wfd_video_codec_get_max_bitrate_kbit (params->selected_codec))
    return reject_profile ("requested-bitrate-exceeds-selected-receiver-codec-limit");
  return TRUE;
}

static void
add_property (JsonBuilder *builder, GstElement *encoder, const gchar *name)
{
  GParamSpec *spec = g_object_class_find_property (G_OBJECT_GET_CLASS (encoder), name);
  json_builder_set_member_name (builder, name);
  if (!spec || !(spec->flags & G_PARAM_READABLE)) {
    json_builder_add_null_value (builder);
    return;
  }
  GValue value = G_VALUE_INIT;
  g_value_init (&value, G_PARAM_SPEC_VALUE_TYPE (spec));
  g_object_get_property (G_OBJECT (encoder), name, &value);
  if (G_VALUE_HOLDS_ENUM (&value)) {
    GEnumClass *klass = g_type_class_ref (G_VALUE_TYPE (&value));
    GEnumValue *item = g_enum_get_value (klass, g_value_get_enum (&value));
    json_builder_add_string_value (builder, item ? item->value_nick : "unknown");
    g_type_class_unref (klass);
  } else if (G_VALUE_HOLDS_BOOLEAN (&value)) {
    json_builder_add_boolean_value (builder, g_value_get_boolean (&value));
  } else if (G_VALUE_HOLDS_UINT (&value)) {
    json_builder_add_int_value (builder, g_value_get_uint (&value));
  } else if (G_VALUE_HOLDS_INT (&value)) {
    json_builder_add_int_value (builder, g_value_get_int (&value));
  } else if (G_VALUE_HOLDS_FLAGS (&value)) {
    json_builder_add_int_value (builder, g_value_get_flags (&value));
  } else {
    json_builder_add_null_value (builder);
  }
  g_value_unset (&value);
}

void
pb_wfd_report_encoder (GstElement *encoder, WfdParams *params)
{
  pb_wfd_telemetry_attach_encoder (encoder);
  g_autoptr(JsonBuilder) builder = new_record ("encoder-configured");
  add_wire (builder, params);
  GstElementFactory *factory = gst_element_get_factory (encoder);
  add_string (builder, "encoder", factory ? gst_plugin_feature_get_name (GST_PLUGIN_FEATURE (factory)) : "unknown");
  add_int (builder, "requested_bitrate_kbps", request.bitrate_set ? request.bitrate_kbps : 0);
  const gchar *properties[] = {"bitrate", "pass", "tune", "speed-preset", "threads",
    "key-int-max", "vbv-buf-capacity", "ref", "cabac", "bframes", "rc-lookahead",
    "sync-lookahead", "sliced-threads", NULL};
  for (guint i = 0; properties[i]; i++) add_property (builder, encoder, properties[i]);
  add_string (builder, "evidence", "configured-properties; actual-throughput-power-latency-unmeasured");
  print_record (builder);
}
