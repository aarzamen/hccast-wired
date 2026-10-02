/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <gst/gst.h>

/* Source lineage, independent of timestamps, buffer addresses and OFFSET.
 * Untagged: the identity survives full-buffer color/size transformations.
 * It describes a whole frame and must not survive a partial byte-range copy. */
typedef struct {
  GstMeta meta;
  guint64 epoch, sequence;
} PbFrameMeta;

PbFrameMeta *pb_frame_meta_add(GstBuffer *buffer, guint64 epoch, guint64 sequence);
const PbFrameMeta *pb_frame_meta_get(GstBuffer *buffer);
