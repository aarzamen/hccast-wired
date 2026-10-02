/* SPDX-License-Identifier: GPL-3.0-or-later */
#include "pb-wfd-profile.h"

PbWfdProfileResult pb_wfd_validate_request (const PbWfdRequest *request)
{
  if (!request) return PB_WFD_PROFILE_UNSUPPORTED_MODE;
  if (request->wire_fields && request->wire_fields != 7)
    return PB_WFD_PROFILE_INCOMPLETE_MODE;
  if (request->wire_fields &&
      (request->fps != 30 || !((request->width == 1280 && request->height == 720) ||
                              (request->width == 1920 && request->height == 1080))))
    return PB_WFD_PROFILE_UNSUPPORTED_MODE;
  if (request->bitrate_set && (request->bitrate_kbps < 512 || request->bitrate_kbps > 8000))
    return PB_WFD_PROFILE_BAD_BITRATE;
  return PB_WFD_PROFILE_OK;
}

PbWfdProfileResult pb_wfd_select_advertised_mode (const PbWfdRequest *request,
    const PbWfdAdvertisedMode *modes, size_t count, unsigned preferred_profile,
    size_t *selected_index, unsigned *selected_profile)
{
  PbWfdProfileResult valid = pb_wfd_validate_request (request);
  if (valid != PB_WFD_PROFILE_OK) return valid;
  if (!request->wire_fields || !selected_index || !selected_profile)
    return PB_WFD_PROFILE_UNSUPPORTED_MODE;
  if (!modes) return PB_WFD_PROFILE_NOT_ADVERTISED;
  if (preferred_profile != 1 && preferred_profile != 2) preferred_profile = 1;
  bool found = false;
  unsigned chosen_profile = 0, chosen_level = 0;
  size_t chosen_index = 0;
  for (size_t i = 0; i < count; i++) {
    const PbWfdAdvertisedMode *mode = &modes[i];
    if (mode->interlaced || mode->width != request->width ||
        mode->height != request->height || mode->fps != request->fps) continue;
    unsigned profile = mode->profile_bits & preferred_profile ? preferred_profile :
                       mode->profile_bits & 1 ? 1 : mode->profile_bits & 2 ? 2 : 0;
    if (!profile) continue;
    if (!found || (chosen_profile != preferred_profile && profile == preferred_profile) ||
        (chosen_profile == profile && mode->level > chosen_level)) {
      chosen_index = i;
      chosen_profile = profile;
      chosen_level = mode->level;
      found = true;
    }
  }
  if (!found) return PB_WFD_PROFILE_NOT_ADVERTISED;
  *selected_index = chosen_index;
  *selected_profile = chosen_profile;
  return PB_WFD_PROFILE_OK;
}

const char *pb_wfd_profile_result_name (PbWfdProfileResult result)
{
  switch (result) {
    case PB_WFD_PROFILE_OK: return "accepted";
    case PB_WFD_PROFILE_INCOMPLETE_MODE: return "wire-width-height-fps-required-together";
    case PB_WFD_PROFILE_UNSUPPORTED_MODE: return "only-progressive-720p30-or-1080p30-supported";
    case PB_WFD_PROFILE_BAD_BITRATE: return "bitrate-must-be-512-to-8000-kbps";
    case PB_WFD_PROFILE_NOT_ADVERTISED: return "requested-progressive-mode-not-advertised";
  }
  return "unknown-profile-error";
}
