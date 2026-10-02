# PanelBridge third-party notices

Build record: 2026-10-01. Original PanelBridge application material is licensed
under **GPL-3.0-or-later**; see [LICENSE](LICENSE) for the grant and complete GPLv3
text. Existing third-party grants and notices remain in force. The repository's
legacy HCCAST material retains its existing MIT license.

This records the reviewed source and dependency notices. Release packaging must
also contain the exact applicable notices and matching source described below.
No public release or complete distribution bundle is claimed here.

## GNOME Network Displays

PanelBridge's native sender incorporates and modifies **GNOME Network Displays
0.99.0**, licensed under GPL-3.0-or-later. Upstream:
[GNOME Network Displays](https://gitlab.gnome.org/GNOME/gnome-network-displays).

- Commit: `eba3ce5dada5f6065e77041c973e73e3c2886bfd`.
- Archive: `vendor/gnome-network-displays-0.99.0.tar.gz`.
- SHA-256: `b6314d25be7589c621b106c1712b00277c9b4d01d4796a78e987f10c0c1d1400`.
- Full original license: `COPYING` inside the pinned upstream source archive.
- Individual source-file notices are preserved in the original archive.

The pinned source headers credit the following authors. This is a summary;
the preserved headers and upstream history retain the complete attribution.

| Credited author | Recorded work |
|---|---|
| Benjamin Berg, 2018 | Sink/provider and P2P implementation; WFD session pool |
| Wim Taymans, 2008 | WFD session-pool source |
| Christian Glombek, 2022, 2024 | MICE/Chromecast and stream sources |
| Anupam Kumar, 2022–2023 | Chromecast and dummy Chromecast sources |
| Pedro Sader Azevedo, 2023–2024 | URI helpers, manager, daemon and stream sources |
| The Chromium Authors, 2019 | `src/cc/cast_channel.proto` |

The worker build includes the common sources and WFD/Chromecast static
libraries. Chromium protocol material and Chromecast notices therefore remain
relevant even though the current app uses wireless display casting only.
Some WFD files have no individual header; the package license is retained without
inventing file-specific authorship. The upstream metadata declares CC0-1.0.
The source archive also contains LGPL-2.1-or-later and LGPL-3.0 notices in its
systemd interface XML, including Red Hat, Inc. and Andrea Scarpino credits.

### PanelBridge changes, 2026-10-01

PanelBridge adds a separate worker, selected-connection adoption, receiver
address checks, explicit display profiles, stream deadlines, metrics and graceful
receiver teardown. The
changes are GPL-3.0-or-later and are kept separately from the original archive:

- `native/wfd/0001-adopt-managed-p2p.patch`
- `native/wfd/0002-explicit-profile.patch`
- `native/wfd/0003-runtime-metrics.patch`
- `native/wfd/0004-graceful-teardown.patch`
- `native/wfd/panelbridge-wfd-worker.c`, `pb-wfd-*.[ch]`, tests and
  `native/wfd/build-worker.sh`

The user supplied direction and physical testing. OpenAI Codex performed the
implementation/review recorded for this build. Earlier model/version authorship
is not recorded. The PanelBridge icon is original project artwork; it is not
the upstream GNOME Network Displays icon.

## Runtime and build dependencies

The Raspberry Pi build uses distro-provided libraries and tools. The table
identifies important components and the terms observed in the installed package
copyright files. Exact notices for an installed package are under
`/usr/share/doc/<package>/copyright`; referenced standard texts are under
`/usr/share/common-licenses`. Preserve both when redistributing those packages.
Source-package notices may cover test files, tools and other outputs with
different licenses; a package-wide keyword list is not a binary license verdict.

| Component / package | Observed version | Applicable terms and role |
|---|---|---|
| GLib/GIO (`libglib2.0-0t64`) | `2.84.4-3~deb13u3` | LGPL-2.1-or-later principal library; retained file-specific permissive notices; event loop and D-Bus |
| GTK4 (`libgtk-4-1`) | `4.18.6+ds-2` | LGPL-2.0-or-later / LGPL-2.1-or-later with file-specific permissive terms; UI and sender string-list objects |
| PyGObject (`python3-gi`) | `3.50.0-4+b1` | LGPL-2.1-or-later; selected files Expat/MIT; Python GI bindings |
| NetworkManager / libnm | `1.52.1-1+rpt4` | Daemon GPL-2.0-or-later; libnm sources LGPL-2.1-or-later; system D-Bus networking and worker inspection |
| GStreamer core | `1.26.2-2` | LGPL-2.0-or-later principal library; retain component-specific notices |
| GStreamer base plugins / libraries | `1.26.2-1+rpt3+deb13u1` | LGPL-family and file-specific permissive terms; raw video, conversion and appsrc |
| GStreamer good plugins | `1.26.2-1+deb13u2` | LGPL-family and file-specific terms; RTP transport |
| GStreamer bad plugins | `1.26.2-3+rpt3+deb13u2` | H.264 parsing uses LGPL-2.0-or-later; MPEG-TS source offers LGPL-compatible alternatives; retain per-file notices |
| GStreamer ugly plugins | `1.26.3-4+deb13u1` | `x264enc` wrapper LGPL-2.0-or-later; its x264 library has separate GPL terms |
| GStreamer RTSP server | `1.26.2-1` | LGPL-2.0-or-later; RTSP server/session handling |
| GStreamer libav plugin | `1.26.2-1+deb13u1` | LGPL-family wrapper; linked FFmpeg keeps its own effective license |
| x264 (`libx264-164`) | `2:0.164.3108+git31e19f9-2+b1` | GPL-2.0-or-later; Copyright 2003–2022 x264 project; H.264 encoder |
| FFmpeg libraries (`libav*`, `libsw*`) | `8:7.1.5-0+deb13u1+rpt1` | Installed package notices describe default binaries as GPL-2.0-or-later and some extra variants as GPL-3.0-or-later; do not describe these binaries as LGPL-only |
| JSON-GLib | `1.10.6+ds-2` | LGPL-2.1-or-later; structured worker events |
| libsoup | `3.6.5-3` | LGPL-family with file-specific terms; upstream common source dependencies |
| protobuf-c | `1.5.1-1` | BSD-2-Clause library; compiler files have separate BSD-3-Clause notices; Chromium protocol support |
| Avahi | `0.8-16` | LGPL-family library notices and separate tool/file terms; upstream discovery dependencies |
| PulseAudio client library | `17.0+dfsg1-2+rpt1` | LGPL-family library notices; upstream common source dependency |
| wf-recorder | `0.5.0-2` | Expat/MIT; separate capture process; its FFmpeg dependencies keep their own terms |
| wlr-randr | `0.4.1-1` | MIT; separate output configuration process |
| labwc | `0.9.8-1+rpt1` | GPL-2.0-only compositor; independent system process, not relicensed by PanelBridge |
| kanshi | `1.5.1-2+b1` | Expat/MIT; independent output-layout tool |
| CPython (`python3.13`) | `3.13.5-2+deb13u4` | Python Software Foundation license and retained component notices; interpreter |
| Pycairo | `1.27.0-2` | LGPL-2.1 OR MPL-1.1; rescue-card and build-time icon rendering use the LGPL option |
| librsvg | `2.60.0+dfsg-1` | LGPL-2.0-or-later principal code, with Rust/per-file permissive terms; build-time icon rendering |
| GCC runtime libraries | `14.2.0-19` | GPL-3.0-or-later with GCC Runtime Library Exception 3.1; retain exception text |

CPython and its standard library retain the Python Software Foundation license
and their individual notices. Build dependencies include Meson/Ninja, compiler
tools, libportal/libadwaita development interfaces, and the Pycairo/librsvg icon
renderer. Pycairo is also a runtime dependency of the independent rescue card.
Build tools are not copied into PanelBridge merely because they are used
to build it. Their exact package notices still apply if they are redistributed.

The complete captured inventory also contains indirect libraries with their own
licenses. Preserve their package copyright records; this short table does not
replace them. GStreamer explicitly distinguishes a plugin wrapper's license from
that of the library it loads. See its
[licensing guidance](https://gstreamer.freedesktop.org/documentation/frequently-asked-questions/licensing.html).
FFmpeg's effective terms depend on its enabled components; see
[FFmpeg licensing](https://ffmpeg.org/legal.html).

For MPEG-TS, the upstream 1.26.2
[muxer](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.2/subprojects/gst-plugins-bad/gst/mpegtsmux/gstmpegtsmux.c)
and [TS library](https://raw.githubusercontent.com/GStreamer/gstreamer/1.26.2/subprojects/gst-plugins-bad/gst/mpegtsmux/tsmux/tsmux.c)
offer MPL-1.1 OR MIT OR LGPL-2.0-or-later. The LGPL option is compatible with this
app's licensing choice. Credits include BBC, Fluendo S.A., Jan Schmidt, Kapil
Agrawal and Julien Moutte. The installed x264 wrapper's notices credit Josef
Zlomek, Michal Benes and Mark Nauwelaerts. The FFmpeg/libjpeg components include
Independent JPEG Group work: this software is based in part on the work of the
Independent JPEG Group.

### wf-recorder and wlr-randr notices

wf-recorder: Copyright © 2019 Ilia Bozhinov. Screencopy protocol: Copyright
© 2018 Simon Ser. Debian packaging: Copyright © 2018–2023 Birger Schacht.

wlr-randr: Copyright 2019 Purism SPC; 2019 emersion; 2019 Guido Gunther.
Debian packaging: Copyright 2020 Henry-Nicolas Tourneur.

The following MIT/Expat terms apply to each of these components under its
respective copyright notice:

> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
> THE SOFTWARE.

## Corresponding source and redistribution

For PanelBridge/GND-derived object code, the GPLv3 terms in [LICENSE](LICENSE),
especially sections 1, 4, 5 and 6, govern distribution. The chosen packaging
approach is to accompany each binary delivery with its matching source, or offer
equivalent source access alongside a binary download under section 6(d).
A link to unmodified upstream source alone does not describe the patched worker.

The source delivery must include:

1. The exact pinned upstream archive, original notices, and all applied patches.
2. The matching PanelBridge Python/native source, interface definitions, tests,
   artwork source, build scripts, compiler/Meson options, and installation,
   service, policy and removal scripts/configuration needed to rebuild and use
   the delivered version.
3. Source for required non-System-Library components, including any distributed
   library/plugin modifications. For each distro component included in this
   source set, retain the exact source-package version, upstream source,
   Debian/Raspberry Pi patches, packaging rules and checksums. A package name,
   copyright file or current upstream URL alone is not corresponding source.
4. Full applicable license/copyright/exception texts and dated modification
   notices. Keep the original MIT notice when legacy material is included.

For a network download, provide clear source directions next to the binary at
no additional charge and keep that source available as required by section 6(d).
For physical delivery, accompany the binary with machine-readable source under
section 6(a). This project has not issued the alternative written source offer;
do not claim one without arranging the section 6(b) obligations.

Retain recipients' rights to modify and redistribute. LGPL components must
retain their license notices and the applicable ability to modify/relink or
replace the library; do not add restrictions against debugging those changes.
Separately redistributed system tools keep their original terms. If a consumer
device is transferred with the software in the circumstances covered by GPLv3
section 6, include the required installation information for modified builds.

Before distribution, install this notice and the license with the app, connect
the About/legal view to those texts and the real matching source location, and
verify the final artifact's contents and hashes. Do not invent a source URL or
claim that the complete source bundle already exists. The retained notices and
software grants do not establish separate codec patent rights or trademark
permission.
