"""Read the optional OLED's choice for this boot without changing system state.

Only installed, root-owned enrollment enables the preference. The selector and
desktop run as the same user; a choice grants no root or remote authority.
Missing hardware, legacy choices and invalid files retain ordinary autostart.
"""

import json
import os
from pathlib import Path
import stat
import uuid


DEVICE = Path('/etc/oled-panel/device.json')
CHOICE = Path('/run/oled-panel/choice.json')
BOOT_ID = Path('/proc/sys/kernel/random/boot_id')


def _read_local(path, owners, limit=4096):
    """Reject redirects, writable-by-other accounts and nonregular files."""
    parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if info.st_uid not in owners or info.st_mode & 0o022:
            raise ValueError('Unsafe preference directory')
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid not in owners \
                    or info.st_mode & 0o022 or info.st_size > limit:
                raise ValueError('Unsafe preference file')
            data = os.read(fd, limit + 1)
            if len(data) > limit:
                raise ValueError('Preference exceeds size limit')
            return json.loads(data)
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def read_startup_choice(device=DEVICE, choice=CHOICE, boot_id=BOOT_ID, *,
                        uid=None, system_uid=0):
    """Return wireless/desktop/headless or None; optional paths support fixtures."""
    uid = os.getuid() if uid is None else uid
    try:
        enrollment = _read_local(Path(device), {system_uid})
        if not isinstance(enrollment, dict) or enrollment.get('panelbridge') is not True:
            return None
        value = _read_local(Path(choice), {system_uid, uid})
        if not isinstance(value, dict) or set(value) != {'mode', 'panelbridge', 'boot_id'} \
                or value['panelbridge'] is not True \
                or value['mode'] not in ('wireless', 'desktop', 'headless'):
            return None
        with Path(boot_id).open() as stream:
            current = stream.read(128).strip()
        if str(uuid.UUID(current)) != current or value['boot_id'] != current:
            return None
        return value['mode']
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None


def autostart_enabled(no_autostart, choice):
    """Explicit CLI suppression wins; an OLED failure never disables startup."""
    return not no_autostart and choice not in ('desktop', 'headless')
