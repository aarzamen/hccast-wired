"""Deterministic Linux ACL and existing VNC authority boundary checks."""
import errno
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools import runtime_access as r

BASE = {(1, 0xffffffff): 7, (4, 0xffffffff): 0, (32, 0xffffffff): 0}
SHARED = {**BASE, (2, 987): 7, (16, 0xffffffff): 7}


def encoded(entries):
    return struct.pack('<I', 2) + b''.join(struct.pack('<HHI', tag, perms, uid) for (tag, uid), perms in entries.items())


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    path = tmp_path / 'runtime'
    path.mkdir(mode=0o700)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    acls = {}
    def getxattr(fd, name):
        if name not in acls:
            raise OSError(errno.ENODATA, 'absent')
        return acls[name]
    monkeypatch.setattr(r.os, 'getxattr', getxattr, raising=False)
    monkeypatch.setattr(r, '_vnc_uid', lambda: 987)
    yield path, fd, acls
    os.close(fd)


def test_private_and_vnc_acl(runtime):
    path, fd, acls = runtime
    assert r.validate_runtime(fd, os.getuid()) == 'private'
    path.chmod(0o770)
    acls['system.posix_acl_access'] = encoded(SHARED)
    assert r.validate_runtime(fd, os.getuid()) == 'existing_vnc'


@pytest.mark.parametrize('entries', [None, BASE, {**SHARED, (2, 986): 7}, {**SHARED, (8, 986): 7},
    {**SHARED, (4, 0xffffffff): 1}, {**SHARED, (32, 0xffffffff): 1},
    {**SHARED, (16, 0xffffffff): 5}, {**BASE, (2, 986): 7, (16, 0xffffffff): 7}])
def test_shared_rejects_unknown_or_nonexclusive_acl(runtime, entries):
    path, fd, acls = runtime
    path.chmod(0o770)
    if entries is not None:
        acls['system.posix_acl_access'] = encoded(entries)
    with pytest.raises(r.RuntimeAccessError):
        r.validate_runtime(fd, os.getuid())


def test_default_and_nonowner_rejected(runtime):
    _, fd, acls = runtime
    with pytest.raises(r.RuntimeAccessError):
        r.validate_runtime(fd, os.getuid() + 1)
    acls['system.posix_acl_default'] = encoded(BASE)
    with pytest.raises(r.RuntimeAccessError):
        r.validate_runtime(fd, os.getuid())


@pytest.mark.parametrize('raw', [b'', b'1234', struct.pack('<I', 2), encoded(BASE) + b'x',
    encoded(BASE) + struct.pack('<HHI', 1, 7, 0xffffffff),
    struct.pack('<IHHI', 2, 2, 7, 0xffffffff), struct.pack('<IHHI', 2, 1, 8, 0xffffffff)])
def test_malformed_acl_rejected(runtime, raw):
    _, fd, acls = runtime
    acls['system.posix_acl_access'] = raw
    with pytest.raises(r.RuntimeAccessError):
        r.validate_runtime(fd, os.getuid())


@pytest.fixture
def authority(monkeypatch):
    account = SimpleNamespace(pw_name='vnc', pw_uid=987, pw_gid=987, pw_shell='/usr/sbin/nologin')
    monkeypatch.setattr(r.pwd, 'getpwnam', lambda name: account)
    monkeypatch.setattr(r.pwd, 'getpwuid', lambda uid: account)
    checked = []
    monkeypatch.setattr(r, '_trusted_file', checked.append)
    values = dict(LoadState='loaded', ActiveState='active', SubState='running', UnitFileState='enabled',
        FragmentPath='/usr/lib/systemd/system/wayvnc.service', DropInPaths='', User='vnc', Group='',
        ExecStart='{ path=/bin/sh ; argv[]=/bin/sh /usr/sbin/wayvnc-run.sh ; ignore_errors=no ; pid=123 ; code=(null) ; status=0/0 }')
    monkeypatch.setattr(r, '_service_show', lambda: values)
    return account, values, checked


def test_dynamic_system_account_and_trusted_files(authority):
    _, _, checked = authority
    assert r._vnc_uid() == 987
    assert checked == ['/etc/passwd', '/usr/lib/systemd/system/wayvnc.service', '/usr/sbin/wayvnc-run.sh']


@pytest.mark.parametrize('field,value', [('ActiveState','inactive'), ('DropInPaths','/etc/override.conf'),
    ('FragmentPath','/etc/systemd/system/wayvnc.service'), ('User','root'), ('Group','other'),
    ('UnitFileState','disabled'), ('ExecStart','{ path=/bin/sh ; argv[]=/bin/sh -c evil ; ignore_errors=no ; pid=1 }')])
def test_service_identity_failures(authority, field, value):
    authority[1][field] = value
    with pytest.raises(r.RuntimeAccessError):
        r._vnc_uid()


@pytest.mark.parametrize('field,value', [('pw_uid', 1000), ('pw_uid', 0), ('pw_gid', 1000), ('pw_shell','/bin/bash'), ('pw_name','other')])
def test_account_identity_failures(authority, field, value):
    setattr(authority[0], field, value)
    with pytest.raises(r.RuntimeAccessError):
        r._vnc_uid()


def test_acl_is_rechecked_each_time(runtime):
    path, fd, acls = runtime
    path.chmod(0o770)
    acls['system.posix_acl_access'] = encoded(SHARED)
    r.validate_runtime(fd, os.getuid())
    acls['system.posix_acl_access'] = encoded({**SHARED, (2, 986): 7})
    with pytest.raises(r.RuntimeAccessError):
        r.validate_runtime(fd, os.getuid())


@pytest.mark.parametrize('reply', ['x' * 20000, 'LoadState=loaded\nLoadState=loaded\n', 'Unknown=value\n'])
def test_service_replies_are_bounded_and_strict(monkeypatch, reply):
    spawn = r.subprocess.Popen
    def fake(args, **kwargs):
        assert args[0] == '/usr/bin/systemctl'
        assert args[-1] == 'wayvnc.service'
        return spawn([sys.executable, '-c', 'import sys; sys.stdout.write(' + repr(reply) + ')'], **kwargs)
    monkeypatch.setattr(r.subprocess, 'Popen', fake)
    with pytest.raises(r.RuntimeAccessError):
        r._service_show()


def test_missing_acl_api_fails_closed(runtime, monkeypatch):
    _, fd, _ = runtime
    monkeypatch.delattr(r.os, 'getxattr')
    with pytest.raises(r.RuntimeAccessError, match='runtime_acl_unavailable'):
        r.validate_runtime(fd, os.getuid())
