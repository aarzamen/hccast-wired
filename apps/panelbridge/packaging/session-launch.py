"""Installed normal-session entry point."""

import sys

if not sys.flags.isolated:
    raise SystemExit("Isolated Python is required")
sys.path.insert(0, "/usr/lib/panelbridge")
from panelbridge.session_service import main

raise SystemExit(main())
