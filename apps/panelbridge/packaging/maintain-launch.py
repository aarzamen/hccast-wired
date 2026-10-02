"""Root-owned installed maintenance entry point. No caller-controlled import root."""

import os
import stat
import sys

if not sys.flags.isolated or os.getuid() != 0 or os.geteuid() != 0:
    raise SystemExit("Use installed isolated Python with real and effective root")

fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    for part in ("usr", "lib", "panelbridge", "packaging_tools"):
        next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
        os.close(fd)
        fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise SystemExit("Installed maintenance import directory is not trusted")
    for name in ("__init__.py", "maintenance.py", "install_transaction.py", "runtime_access.py"):
        info = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1
                or info.st_mode & 0o022 or info.st_size > 524288):
            raise SystemExit("Installed maintenance module is not trusted")
finally:
    os.close(fd)

sys.path.insert(0, "/usr/lib/panelbridge")
from packaging_tools.maintenance import main  # noqa: E402 - trust checks must precede application imports

raise SystemExit(main())
