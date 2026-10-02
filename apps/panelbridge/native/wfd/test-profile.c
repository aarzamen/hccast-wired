/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <stdio.h>
#include "pb-wfd-profile.h"
#define CHECK(expr) do { if (!(expr)) { fprintf(stderr, "FAIL line %d: %s\n", __LINE__, #expr); return 1; } } while (0)

int main (void)
{
  PbWfdRequest request = { .wire_fields=7, .width=1280, .height=720, .fps=30 };
  /* Real protocol alternatives represented after GND decodes codec bitmaps. */
  PbWfdAdvertisedMode modes[] = {
    {1920,1080,30,false,1,4,0},
    {1280,720,60,false,1,4,0},
    {1280,720,30,true,1,4,0},
    {1280,720,30,false,2,8,1},
    {1280,720,30,false,1,1,2},
    {1280,720,30,false,1,2,3},
  };
  size_t selected = 99;
  unsigned profile = 0;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_OK);
  CHECK(pb_wfd_select_advertised_mode(&request,modes,6,1,&selected,&profile) == PB_WFD_PROFILE_OK);
  CHECK(selected == 5 && profile == 1); /* Baseline, highest compatible level. */
  CHECK(pb_wfd_select_advertised_mode(&request,modes,3,1,&selected,&profile) == PB_WFD_PROFILE_NOT_ADVERTISED);
  CHECK(pb_wfd_select_advertised_mode(&request,modes+3,1,1,&selected,&profile) == PB_WFD_PROFILE_OK);
  CHECK(selected == 0 && profile == 2); /* Supported high-only alternative. */
  request.width=1920; request.height=1080;
  CHECK(pb_wfd_select_advertised_mode(&request,modes,6,1,&selected,&profile) == PB_WFD_PROFILE_OK);
  CHECK(selected == 0);
  request.fps=15;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_UNSUPPORTED_MODE);
  request.fps=30; request.width=1366; request.height=768;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_UNSUPPORTED_MODE);
  request.width=1280; request.height=720; request.wire_fields=3;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_INCOMPLETE_MODE);
  request.wire_fields=0;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_OK);
  CHECK(pb_wfd_select_advertised_mode(&request,modes,6,1,&selected,&profile) == PB_WFD_PROFILE_UNSUPPORTED_MODE);
  request.bitrate_set=true;
  for (int rate=512; rate<=8000; rate+=7488) {
    request.bitrate_kbps=rate;
    CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_OK);
  }
  request.bitrate_kbps=511;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_BAD_BITRATE);
  request.bitrate_kbps=8001;
  CHECK(pb_wfd_validate_request(&request) == PB_WFD_PROFILE_BAD_BITRATE);
  request.bitrate_set=false; request.wire_fields=7;
  CHECK(pb_wfd_select_advertised_mode(&request,NULL,0,1,&selected,&profile) == PB_WFD_PROFILE_NOT_ADVERTISED);
  PbWfdAdvertisedMode combined={1280,720,30,false,3,2,0};
  CHECK(pb_wfd_select_advertised_mode(&request,&combined,1,1,&selected,&profile) == PB_WFD_PROFILE_OK);
  CHECK(profile == 1);
  combined.profile_bits=4;
  CHECK(pb_wfd_select_advertised_mode(&request,&combined,1,1,&selected,&profile) == PB_WFD_PROFILE_NOT_ADVERTISED);
  puts("PASS: advertised progressive mode intersection and bitrate bounds");
  return 0;
}
