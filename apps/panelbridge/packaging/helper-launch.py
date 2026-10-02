"""Fixed installed entry point; invoked only with Python isolated mode."""

import sys

if not sys.flags.isolated:
    raise SystemExit("Isolated Python is required")
sys.path.insert(0, "/usr/lib/panelbridge")
from helper.service import main

raise SystemExit(main())
