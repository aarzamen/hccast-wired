/* SPDX-License-Identifier: GPL-3.0-or-later */
#ifndef PB_WFD_PROFILE_H
#define PB_WFD_PROFILE_H
#include <stddef.h>
#include <stdbool.h>

typedef struct {
  unsigned wire_fields; /* width=1, height=2, fps=4; either 0 or 7 */
  int width, height, fps;
  bool bitrate_set;
  int bitrate_kbps;
} PbWfdRequest;

/* A mode decoded from one receiver-advertised H.264 codec. */
typedef struct {
  int width, height, fps;
  bool interlaced;
  unsigned profile_bits, level;
  size_t codec_index;
} PbWfdAdvertisedMode;

typedef enum {
  PB_WFD_PROFILE_OK,
  PB_WFD_PROFILE_INCOMPLETE_MODE,
  PB_WFD_PROFILE_UNSUPPORTED_MODE,
  PB_WFD_PROFILE_BAD_BITRATE,
  PB_WFD_PROFILE_NOT_ADVERTISED
} PbWfdProfileResult;

PbWfdProfileResult pb_wfd_validate_request (const PbWfdRequest *request);
PbWfdProfileResult pb_wfd_select_advertised_mode (const PbWfdRequest *request,
    const PbWfdAdvertisedMode *modes, size_t count, unsigned preferred_profile,
    size_t *selected_index, unsigned *selected_profile);
const char *pb_wfd_profile_result_name (PbWfdProfileResult result);
#endif
