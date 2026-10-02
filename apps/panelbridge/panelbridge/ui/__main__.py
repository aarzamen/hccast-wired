"""Run with the supported image's distro Python and GTK4/PyGObject packages."""

import sys


def run() -> int:
    try:
        from .app import main
    except (ImportError, ValueError) as error:
        if "gi" not in str(error).lower() and "gtk" not in str(error).lower():
            raise
        print("PanelBridge needs GTK4 and PyGObject from the supported Raspberry Pi image. "
              "Launch the installed app with distro Python.", file=sys.stderr)
        return 2
    return main()


raise SystemExit(run())
