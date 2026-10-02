/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <gst/gst.h>
void pb_wfd_telemetry_attach_source(GstElement *source, GstElement *rate);
void pb_wfd_telemetry_attach_encoder(GstElement *encoder);
void pb_wfd_telemetry_raw_pushed(void);
void pb_wfd_telemetry_calibration_configure(guint warmup_ms, guint duration_ms);
void pb_wfd_telemetry_calibration_streaming(void);
/* begin returns FALSE at the complete-frame boundary that starts EOS drain. */
gboolean pb_wfd_telemetry_raw_begin(GstBuffer *buffer, guint64 *sequence);
void pb_wfd_telemetry_raw_result(guint64 sequence, GstFlowReturn flow);
gboolean pb_wfd_telemetry_calibration_closed(gboolean *complete);
void pb_wfd_telemetry_calibration_abort(const char *reason);
guint64 pb_wfd_telemetry_raw_frames(void);
void pb_wfd_telemetry_emit(gboolean final);
void pb_wfd_telemetry_clear(void);
