"""Optional same-host OLED choices cannot strand normal desktop startup."""

import json
import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panelbridge.startup_choice import read_startup_choice, autostart_enabled

BOOT = '11111111-2222-4333-8444-555555555555'


@pytest.fixture
def files(tmp_path):
    etc, run = tmp_path / 'etc', tmp_path / 'run'
    etc.mkdir(mode=0o755)
    run.mkdir(mode=0o755)
    device, choice, boot = etc / 'device.json', run / 'choice.json', tmp_path / 'boot_id'
    device.write_text(json.dumps({'panelbridge': True}))
    choice.write_text(json.dumps({'panelbridge': True, 'mode': 'desktop', 'boot_id': BOOT}))
    device.chmod(0o644)
    choice.chmod(0o644)
    boot.write_text(BOOT + '\n')
    return device, choice, boot


def read(files):
    return read_startup_choice(*files, uid=os.getuid(), system_uid=os.getuid())


@pytest.mark.parametrize('mode', ['wireless', 'desktop', 'headless'])
def test_enrolled_choice_is_scoped_to_current_boot(files, mode):
    files[1].write_text(json.dumps({'panelbridge': True, 'mode': mode, 'boot_id': BOOT}))
    assert read(files) == mode
    assert autostart_enabled(False, mode) == (mode == 'wireless')
    assert not autostart_enabled(True, mode)


@pytest.mark.parametrize('index', [0, 1, 2])
def test_missing_or_corrupt_files_preserve_autostart(files, index):
    files[index].write_text('broken')
    assert read(files) is None
    files[index].unlink()
    assert autostart_enabled(False, read(files))


@pytest.mark.parametrize('payload', [
    {'mode': 'headless'}, {'panelbridge': 1, 'mode': 'desktop', 'boot_id': BOOT},
    {'panelbridge': True, 'mode': 'desktop', 'boot_id': 'old-boot'},
    {'panelbridge': True, 'mode': 'erase', 'boot_id': BOOT},
    {'panelbridge': True, 'mode': [], 'boot_id': BOOT},
    {'panelbridge': True, 'mode': 'desktop', 'boot_id': BOOT, 'extra': 'ignored?'},
    [], None,
])
def test_legacy_stale_and_invalid_choices_do_not_suppress(files, payload):
    files[1].write_text(json.dumps(payload))
    assert read(files) is None


@pytest.mark.parametrize('enrollment', [False, 1, 'true', None])
def test_enrollment_requires_boolean_true(files, enrollment):
    files[0].write_text(json.dumps({'panelbridge': enrollment}))
    assert read(files) is None


@pytest.mark.parametrize('index', [0, 1])
def test_symlink_group_writes_and_large_files_are_rejected(files, index):
    path = files[index]
    path.chmod(0o664)
    assert read(files) is None
    path.chmod(0o644)
    path.parent.chmod(0o777)
    assert read(files) is None
    path.parent.chmod(0o755)
    target = path.with_name('target')
    path.rename(target)
    path.symlink_to(target)
    assert read(files) is None
    path.unlink()
    path.write_text(' ' * 4097)
    assert read(files) is None


def test_foreign_owner_and_fifo_are_rejected(files):
    assert read_startup_choice(*files, uid=os.getuid(), system_uid=os.getuid() + 123) is None
    files[1].unlink()
    os.mkfifo(files[1])
    assert read(files) is None


def test_no_cli_autostart_is_never_overridden():
    for choice in (None, 'wireless', 'desktop', 'headless'):
        assert not autostart_enabled(True, choice)
