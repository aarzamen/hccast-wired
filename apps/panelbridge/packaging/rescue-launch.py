"""Root-owned installed entry point, executed by the dedicated nonroot account."""

import sys

if not sys.flags.isolated:
    raise SystemExit("Isolated Python is required")
sys.path.insert(0, "/usr/lib/panelbridge")
from panelbridge.rescue_service import main

raise SystemExit(main())
