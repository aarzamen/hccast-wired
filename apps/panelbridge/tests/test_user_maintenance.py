"""Normal-user CLI boundaries and real temporary configuration transactions."""

import copy
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
OUTPUT = [{"name": "HDMI-A-1", "enabled": True, "scale": 1,
           "transform": "270", "position": {"x": 0, "y": 0},
           "modes": [{"width": 400, "height": 1280, "refresh": 59.506, "current": True}],
           "description": "PRIVATE DEVICE DESCRIPTION"}]


@pytest.fixture
def module():
    # A missing seam is an explicit failing assertion in the first red run.
    assert importlib.util.find_spec("packaging_tools.user_maintenance") is not None
    return importlib.import_module("packaging_tools.user_maintenance")


@pytest.fixture
def account(module, tmp_path, monkeypatch):
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_uid=uid, pw_dir=str(home)))
    monkeypatch.setenv("HOME", str(tmp_path / "spoofed-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "spoofed-config"))
    return home


@pytest.fixture
def desktop(module, monkeypatch):
    monkeypatch.setattr(module, "_desktop_environment", lambda uid: {"WAYLAND_DISPLAY": "wayland-0"})
    monkeypatch.setattr(module, "_capture", lambda env: json.dumps(OUTPUT).encode())


def invoke(module, capsys, action):
    code = module.main([action])
    captured = capsys.readouterr()
    assert captured.err == ""
    return code, json.loads(captured.out)


def tree(path):
    return {str(p.relative_to(path)): (p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
            for p in path.rglob("*") if p.is_file()}


def test_status_and_empty_restore_do_not_create_config(module, account, capsys):
    for action in ("status", "restore"):
        code, receipt = invoke(module, capsys, action)
        assert code == 0 and receipt["state"] == "not_configured"
        assert receipt["live_output_restored"] is None
        assert list(account.iterdir()) == []


def test_round_trip_preserves_originals_modes_and_user_edits(module, account, desktop, capsys):
    base = account / ".config"
    for name, data in (("labwc/environment", b"XKB_DEFAULT_LAYOUT=us\n"),
                       ("kanshi/config", b"# my display configuration\n")):
        path = base / name
        path.parent.mkdir(parents=True, mode=0o700)
        path.write_bytes(data)
        path.chmod(0o640)
    code, configured = invoke(module, capsys, "configure")
    assert code == 0 and configured["state"] == "configured"
    assert configured["scope"] == "configuration_bytes_only"
    env = base / "labwc/environment"
    assert b"WLR_HEADLESS_OUTPUTS=1" in env.read_bytes()
    assert "position 1280,0" in (base / "kanshi/config").read_text()
    assert (base / "autostart/panelbridge-session.desktop").is_file()
    journal = (base / "panelbridge/desktop-install.json").read_bytes()
    env.write_bytes(env.read_bytes() + b"USER_SETTING=1\n")
    assert invoke(module, capsys, "configure")[0] == 0
    assert (base / "panelbridge/desktop-install.json").read_bytes() == journal
    code, restored = invoke(module, capsys, "restore")
    assert code == 0 and restored["state"] == "restored"
    assert restored["scope"] == "configuration_bytes_only"
    assert restored["live_output_restored"] is None
    assert env.read_bytes() == b"XKB_DEFAULT_LAYOUT=us\nUSER_SETTING=1\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o640
    assert (base / "kanshi/config").read_bytes() == b"# my display configuration\n"
    assert not (base / "autostart/panelbridge-session.desktop").exists()
    rendered = json.dumps([configured, restored])
    for private in (str(account), "PRIVATE DEVICE DESCRIPTION", "XKB_DEFAULT", "HDMI-A-1", "pw_uid"):
        assert private not in rendered
    assert not (account.parent / "spoofed-home").exists()
    assert not (account.parent / "spoofed-config").exists()


def test_restore_needs_no_live_desktop_and_conflicts_preserve_all_files(module, account, desktop, monkeypatch, capsys):
    assert invoke(module, capsys, "configure")[0] == 0
    base = account / ".config"
    autostart = base / "autostart/panelbridge-session.desktop"
    autostart.write_bytes(autostart.read_bytes() + b"UserEdit=true\n")
    before = tree(base)
    monkeypatch.setattr(module, "_desktop_environment", lambda uid: pytest.fail("Restore/status must not inspect desktop"))
    for action in ("restore", "status"):
        code, receipt = invoke(module, capsys, action)
        assert code == 3 and receipt["state"] == "conflict"
        assert receipt["conflict_count"] == 1
        assert tree(base) == before


def test_successful_restore_and_status_do_not_query_compositor(module, account, desktop, monkeypatch, capsys):
    assert invoke(module, capsys, "configure")[0] == 0
    monkeypatch.setattr(module, "_desktop_environment", lambda uid: pytest.fail("No desktop query permitted"))
    before = tree(account)
    assert invoke(module, capsys, "status")[1]["state"] == "configured"
    assert tree(account) == before
    code, receipt = invoke(module, capsys, "restore")
    assert code == 0 and receipt["state"] == "restored"
    assert receipt["live_output_restored"] is None
    assert not (account / ".config/labwc/environment").exists()


def test_capture_start_failure_never_mutates_configuration(module, account, monkeypatch, capsys):
    monkeypatch.setattr(module, "_desktop_environment", lambda uid: {"LC_ALL": "C"})
    def missing_tool(*args, **kwargs):
        raise FileNotFoundError("/private/caller/path")
    monkeypatch.setattr(module.subprocess, "Popen", missing_tool)
    code, receipt = invoke(module, capsys, "configure")
    assert code == 4 and receipt["reason"] == "capture_unavailable"
    assert "private" not in json.dumps(receipt)
    assert list(account.iterdir()) == []


def test_missing_desktop_never_creates_state(module, account, monkeypatch, capsys):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert invoke(module, capsys, "configure")[1]["state"] == "desktop_unavailable"
    assert list(account.iterdir()) == []


def test_symlink_config_does_not_read_or_mutate_the_target(module, account, tmp_path, capsys):
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    (foreign / "untouched").write_bytes(b"PRIVATE")
    (account / ".config").symlink_to(foreign)
    for action in ("status", "restore"):
        assert invoke(module, capsys, action)[1]["state"] == "conflict"
    assert tree(foreign) == {"untouched": (b"PRIVATE", stat.S_IMODE((foreign / "untouched").stat().st_mode))}


def test_transaction_constructor_refusal_is_reported_without_traceback(module, account, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise RuntimeError("PRIVATE ERROR")
    monkeypatch.setattr(module, "UserSetup", refuse)
    code, receipt = invoke(module, capsys, "status")
    assert code == 3 and receipt["state"] == "conflict"
    assert "PRIVATE" not in json.dumps(receipt)


@pytest.mark.parametrize("raw", [b"", b"null", b"{}", b"[]", b"[null]", b"\xff", b"[", b"[NaN]",
                                    b"[" * 2000, b" " * 262145,
                                    json.dumps(OUTPUT).replace('"enabled": true', '"enabled": false, "enabled": true').encode()])
def test_invalid_capture_never_creates_config(module, account, desktop, capsys, monkeypatch, raw):
    monkeypatch.setattr(module, "_capture", lambda env: raw)
    code, receipt = invoke(module, capsys, "configure")
    assert code == 4 and receipt["state"] == "desktop_unavailable"
    assert not (account / ".config").exists()


@pytest.mark.parametrize("change", [
    lambda data: data[0].update(enabled=False),
    lambda data: data[0].update(enabled=1),
    lambda data: data[0].update(name="output\ncommand"),
    lambda data: data[0].update(scale=float("inf")),
    lambda data: data[0].update(scale=0),
    lambda data: data[0].pop("position"),
    lambda data: data[0].update(position={"x": True, "y": 0}),
    lambda data: data[0].update(transform="unknown"),
    lambda data: data[0]["modes"][0].update(width=0),
    lambda data: data[0]["modes"][0].update(width=1.5),
    lambda data: data[0]["modes"][0].update(refresh=True),
    lambda data: data[0]["modes"][0].update(current=False),
    lambda data: data[0]["modes"].append(dict(data[0]["modes"][0])),
    lambda data: data.append(copy.deepcopy(data[0])),
])
def test_bad_output_baseline_preserves_existing_config(module, account, desktop, monkeypatch, capsys, change):
    base = account / ".config"
    base.mkdir()
    (base / "untouched").write_bytes(b"unchanged")
    before = tree(base)
    data = copy.deepcopy(OUTPUT)
    change(data)
    monkeypatch.setattr(module, "_capture", lambda env: json.dumps(data).encode())
    assert invoke(module, capsys, "configure")[1]["state"] == "desktop_unavailable"
    assert tree(base) == before and list(base.iterdir()) == [base / "untouched"]


@pytest.mark.parametrize("uid,euid", [(0, 0), (0, 1234), (1234, 0), (1234, 1235)])
def test_root_or_mismatched_identity_cannot_even_read_state(module, account, monkeypatch, capsys, uid, euid):
    monkeypatch.setattr(module.os, "getuid", lambda: uid)
    monkeypatch.setattr(module.os, "geteuid", lambda: euid)
    for action in ("configure", "restore", "status"):
        assert invoke(module, capsys, action) == (5, {
            "api_version": 1, "action": action, "state": "identity_refused", "reason": "normal_user_required",
            "scope": "configuration_bytes_only", "live_output_restored": None})
    assert list(account.iterdir()) == []


@pytest.mark.parametrize("home", ["", ".", "relative/home", "/tmp/../home", "/"])
def test_bad_passwd_home_is_rejected_without_fallback(module, account, monkeypatch, capsys, home):
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_uid=uid, pw_dir=home))
    assert invoke(module, capsys, "status")[1]["state"] == "identity_refused"
    assert list(account.iterdir()) == []


@pytest.mark.parametrize("env", [{}, {"WAYLAND_DISPLAY": "wayland-0"},
    {"XDG_RUNTIME_DIR": "/tmp/foreign", "WAYLAND_DISPLAY": "wayland-0"},
    {"XDG_RUNTIME_DIR": "/run/user/4242", "WAYLAND_DISPLAY": "../foreign"},
    {"XDG_RUNTIME_DIR": "/run/user/4242", "WAYLAND_DISPLAY": "/tmp/foreign"},
    {"XDG_RUNTIME_DIR": "/run/user/4242", "WAYLAND_DISPLAY": "wayland-0", "XDG_SESSION_TYPE": "x11"}])
def test_missing_or_spoofed_desktop_environment_fails_closed(module, monkeypatch, env):
    monkeypatch.setattr(module.os, "environ", env)
    with pytest.raises(module.DesktopUnavailable):
        module._desktop_environment(4242)


def test_runtime_environment_uses_only_owned_socket_and_fixed_allowlist(module, monkeypatch):
    # Unix socket names on macOS are limited to 104 bytes, shorter than pytest's
    # normal temporary path. This still uses an owned disposable directory.
    temporary = tempfile.TemporaryDirectory(prefix="pb-user-", dir="/tmp")
    runtime = Path(temporary.name).resolve()
    server = socket.socket(socket.AF_UNIX)
    try:
        server.bind(str(runtime / "wayland-0"))
        uid = os.getuid()
        monkeypatch.setattr(module, "_runtime_directory", lambda value: runtime)
        monkeypatch.setattr(module.os, "environ", {
            "XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-0",
            "LD_PRELOAD": "/private/spoof", "PYTHONPATH": "/private/spoof", "PATH": "/private/spoof",
            "HOME": "/private/spoof", "WLR_BACKENDS": "spoof", "XDG_SESSION_TYPE": "wayland"})
        env = module._desktop_environment(uid)
        assert env == {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "XDG_SESSION_TYPE": "wayland",
                       "XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": "wayland-0"}
        runtime.chmod(0o777)
        with pytest.raises(module.DesktopUnavailable):
            module._desktop_environment(uid)
        runtime.chmod(0o700)
        (runtime / "wayland-0").unlink()
        (runtime / "wayland-0").write_bytes(b"not a socket")
        with pytest.raises(module.DesktopUnavailable):
            module._desktop_environment(uid)
    finally:
        server.close()
        temporary.cleanup()


@pytest.mark.parametrize("script,reason", [
    ("import os; os.write(1, b'x' * 262145)", "capture_too_large"),
    ("import time; time.sleep(10)", "capture_timeout"),
    ("import sys; sys.stderr.write('PRIVATE ERROR'); sys.exit(1)", "capture_failed"),
])
def test_real_capture_process_has_bounded_output_and_deadline(module, monkeypatch, script, reason):
    spawn = subprocess.Popen
    processes = []
    def fixture_process(args, **kwargs):
        assert args == ["/usr/bin/wlr-randr", "--json"]
        assert kwargs["env"] == {"LC_ALL": "C"}
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["stderr"] == subprocess.DEVNULL
        process = spawn([sys.executable, "-I", "-B", "-c", script], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(module.subprocess, "Popen", fixture_process)
    monkeypatch.setattr(module, "CAPTURE_TIMEOUT", 0.15)
    started = time.monotonic()
    with pytest.raises(module.DesktopUnavailable) as error:
        module._capture({"LC_ALL": "C"})
    assert error.value.reason == reason
    assert time.monotonic() - started < 2
    assert all(process.poll() is not None for process in processes)


def test_capture_success_returns_bytes_without_stderr(module, monkeypatch):
    spawn = subprocess.Popen
    monkeypatch.setattr(module.subprocess, "Popen", lambda args, **kwargs: spawn(
        [sys.executable, "-I", "-B", "-c", "import sys; sys.stdout.write('[]'); sys.stderr.write('PRIVATE')"], **kwargs))
    assert module._capture({"LC_ALL": "C"}) == b"[]"


def test_cli_rejects_path_command_and_fixture_flags_without_echoing_input(module, account, capsys):
    for args in ([], ["configure", "--home", "/private/secret"], ["restore", "--fixture"],
                 ["status", "--command", "anything"], ["/private/secret"]):
        assert module.main(args) == 2
        captured = capsys.readouterr()
        assert json.loads(captured.out)["state"] == "invalid_request"
        assert "/private/secret" not in captured.out + captured.err
        assert list(account.iterdir()) == []


def test_installed_launcher_enforces_isolation_and_fixed_import_root(tmp_path):
    launcher = APP / "packaging/maintain-user-launch.py"
    assert launcher.is_file()
    home = tmp_path / "account"
    home.mkdir(mode=0o700)
    ordinary = subprocess.run([sys.executable, "-B", str(launcher), "status"], capture_output=True, text=True)
    assert ordinary.returncode != 0
    harness = '''
import importlib.abc, importlib.util, pathlib, pwd, runpy, sys, types
app, launcher, home = sys.argv[1:]
class InstalledRoot(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "packaging_tools":
            assert sys.path[0] == "/usr/lib/panelbridge"
            return importlib.util.spec_from_file_location(fullname, pathlib.Path(app) / "packaging_tools/__init__.py", submodule_search_locations=[str(pathlib.Path(app) / "packaging_tools")])
sys.meta_path.insert(0, InstalledRoot())
pwd.getpwuid = lambda uid: types.SimpleNamespace(pw_uid=uid, pw_dir=home)
sys.argv = [launcher, "status"]
runpy.run_path(launcher, run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-I", "-B", "-c", harness, str(APP), str(launcher), str(home)],
                            capture_output=True, text=True, env={"PYTHONPATH": str(tmp_path), "HOME": "/not-home"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["state"] == "not_configured"
    assert list(home.iterdir()) == []


@pytest.fixture(autouse=True)
def linux_acl_fixture_on_mac(monkeypatch):
    # Deployment is Linux. macOS Python lacks Linux ACL xattrs; emulate only
    # the absent Linux ACL for existing filesystem fixtures, never production.
    if not hasattr(os, "getxattr"):
        import errno
        from packaging_tools import runtime_access
        def absent(fd, name):
            raise OSError(errno.ENODATA, "absent fixture Linux ACL")
        monkeypatch.setattr(runtime_access.os, "getxattr", absent, raising=False)


def test_user_capture_rechecks_shared_runtime_authority(module, monkeypatch):
    with tempfile.TemporaryDirectory(prefix='pb-acl-', dir='/tmp') as directory:
        runtime = Path(directory).resolve()
        server = socket.socket(socket.AF_UNIX)
        try:
            server.bind(str(runtime / 'wayland-0'))
            monkeypatch.setattr(module, '_runtime_directory', lambda uid: runtime)
            monkeypatch.setenv('XDG_RUNTIME_DIR', str(runtime))
            monkeypatch.setenv('WAYLAND_DISPLAY', 'wayland-0')
            monkeypatch.setenv('XDG_SESSION_TYPE', 'wayland')
            calls = []
            def validate(fd, uid):
                calls.append(uid)
                if len(calls) > 1:
                    raise module.RuntimeAccessError('vnc authority changed')
            monkeypatch.setattr(module, 'validate_runtime', validate)
            module._desktop_environment(os.getuid())
            with pytest.raises(module.DesktopUnavailable):
                module._desktop_environment(os.getuid())
            assert calls == [os.getuid(), os.getuid()]
        finally:
            server.close()
