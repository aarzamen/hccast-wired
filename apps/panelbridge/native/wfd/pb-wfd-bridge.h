/* SPDX-License-Identifier: GPL-3.0-or-later */
#pragma once
#include <gst/gst.h>
#include "wfd-params.h"
#include "pb-wfd-profile.h"

void pb_wfd_configure_request (const PbWfdRequest *request,
    gint source_width, gint source_height, gint source_fps);
gboolean pb_wfd_has_wire_request (void);
guint pb_wfd_requested_bitrate (void);
gboolean pb_wfd_select_requested_mode (WfdParams *params, guint preferred_profile);
gboolean pb_wfd_validate_selected_bitrate (WfdParams *params);
void pb_wfd_report_encoder (GstElement *encoder, WfdParams *params);
