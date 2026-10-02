/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "pb-wfd-frame-meta.h"

static GType frame_meta_api(void) {
  static gsize api;
  static const gchar *tags[]={NULL};
  if(g_once_init_enter(&api)) {
    GType type=gst_meta_api_type_register("PanelBridgeFrameIdentityAPI",tags);
    g_once_init_leave(&api,type);
  }
  return (GType)api;
}
static gboolean initialize(GstMeta *meta,gpointer params,GstBuffer *buffer) {
  PbFrameMeta *frame=(PbFrameMeta *)meta;(void)params;(void)buffer;
  frame->epoch=0;frame->sequence=0;return TRUE;
}
static gboolean transform(GstBuffer *dest,GstMeta *meta,GstBuffer *source,GQuark type,gpointer data) {
  const PbFrameMeta *frame=(const PbFrameMeta *)meta;
  (void)source;
  if(!GST_META_TRANSFORM_IS_COPY(type))return FALSE;
  const GstMetaTransformCopy *copy=data;
  if(copy && copy->region)return FALSE;
  return pb_frame_meta_add(dest,frame->epoch,frame->sequence)!=NULL;
}
static const GstMetaInfo *frame_meta_info(void) {
  static gsize info;
  if(g_once_init_enter(&info)) {
    const GstMetaInfo *registered=gst_meta_register(frame_meta_api(),"PanelBridgeFrameIdentity",
        sizeof(PbFrameMeta),initialize,NULL,transform);
    g_once_init_leave(&info,(gsize)registered);
  }
  return (const GstMetaInfo *)info;
}
PbFrameMeta *pb_frame_meta_add(GstBuffer *buffer,guint64 epoch,guint64 sequence) {
  if(!buffer || !gst_buffer_is_writable(buffer) || pb_frame_meta_get(buffer))return NULL;
  PbFrameMeta *frame=(PbFrameMeta *)gst_buffer_add_meta(buffer,frame_meta_info(),NULL);
  if(frame) { frame->epoch=epoch;frame->sequence=sequence; }
  return frame;
}
const PbFrameMeta *pb_frame_meta_get(GstBuffer *buffer) {
  return buffer ? (const PbFrameMeta *)gst_buffer_get_meta(buffer,frame_meta_api()) : NULL;
}
