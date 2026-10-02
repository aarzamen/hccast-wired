"""Installed normal-user configuration entry point; invoke with python3 -I -B."""

import sys

if not sys.flags.isolated:
    raise SystemExit("Isolated Python is required")
sys.path.insert(0, "/usr/lib/panelbridge")
from packaging_tools.user_maintenance import main

raise SystemExit(main())
