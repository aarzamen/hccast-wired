"""Attribution from the retained build and package-license records."""

from pathlib import Path


def about_record():
    installed = Path("/usr/share/panelbridge")
    source_tree = Path(__file__).resolve().parents[1]
    legal_root = installed if (installed / "THIRD_PARTY_NOTICES.md").is_file() else source_tree
    notices = legal_root / "THIRD_PARTY_NOTICES.md"
    license_file = legal_root / "LICENSE"
    return {
        "name": "PanelBridge",
        "version": "0.1.0 development",
        "credits": [
            {"name": "Project owner", "role": "Product direction, hardware, test decisions and physical panel validation"},
            {"name": "OpenAI Codex", "role": "Architecture, implementation, integration, tests, review and original icon in this recorded build"},
            {"name": "Model attribution", "role": "Exact model/version history is not recorded in the repository; earlier contributions remain unattributed"},
            {"name": "Development tools", "role": "Python and pytest for software checks; Ruff for linting; GCC, Meson and Ninja for the native sender; SSH for Pi deployment and verification"},
            {"name": "GNOME Network Displays contributors", "role": "Upstream wireless display implementation; individual author and file notices are preserved below"},
        ],
        "licenses": [
            {"component": "PanelBridge and modified GNOME Network Displays 0.99.0", "license": "GPL-3.0-or-later"},
            {"component": "GTK4, GLib/GIO, PyGObject, GStreamer and plugin wrappers", "license": "LGPL-family licenses and retained per-file notices"},
            {"component": "x264 and the installed FFmpeg build", "license": "GPL-2.0-or-later; exact component notices below"},
            {"component": "NetworkManager", "license": "GPL-2.0-or-later daemon; LGPL-2.1-or-later libnm"},
            {"component": "wf-recorder, wlr-randr and kanshi", "license": "MIT/Expat"},
            {"component": "labwc (separate system compositor)", "license": "GPL-2.0-only"},
            {"component": "CPython", "license": "Python Software Foundation and component notices"},
            {"component": "Pycairo", "license": "LGPL-2.1 option; upstream also offers MPL-1.1"},
            {"component": "Full dependency notices", "text": notices.read_text() if notices.is_file() else "Not installed; this build is incomplete"},
            {"component": "Application license", "text": license_file.read_text() if license_file.is_file() else "Not installed; this build is incomplete"},
        ],
        "source": "Development checkout. A matching source bundle and final install artifact are still being assembled; no release download is claimed.",
        "note": "Independent project. Not affiliated with or endorsed by the screen manufacturer. Human direction and physical validation are credited separately from AI-generated work.",
    }
