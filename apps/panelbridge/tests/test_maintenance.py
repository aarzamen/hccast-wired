"""Root lifecycle in a private unprivileged tree with controlled system replies."""

import importlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class Crash(BaseException):
    pass


class System:
    def __init__(self):
        self.active = self.enabled = self.rescue = self.session_owner = self.media = False
        self.failed = False
        self.graphical = True
        self.configured = False
        self.calls = []
        self.fail = None
        self.crash = None
        self.user_conflict = False
        self.helper_idle = True

    def __call__(self, args, *, env, timeout=8):
        self.calls.append((tuple(args), dict(env)))
        assert "LD_PRELOAD" not in env and "PYTHONPATH" not in env
        if args[0] == "/usr/sbin/runuser":
            assert args[:5] == ["/usr/sbin/runuser", "--user", "fixture-user", "--", "/usr/bin/env"]
            assert "-i" in args and "HOME=" + self.home in args
            if "/usr/lib/panelbridge/maintain-user-launch.py" in args:
                action = args[-1]
                if self.user_conflict:
                    return 3, json.dumps({"api_version": 1, "action": action, "state": "conflict",
                                         "scope": "configuration_bytes_only", "live_output_restored": None})
                if action == "configure":
                    self.configured = True
                elif action == "restore":
                    self.configured = False
                state = "configured" if self.configured else "restored"
                return 0, json.dumps({"api_version": 1, "action": action, "state": state,
                                     "scope": "configuration_bytes_only", "live_output_restored": None})
            if "NameHasOwner" in args:
                return 0, json.dumps({"type": "b", "data": [self.session_owner]})
            if "Status" in args:
                return 0, json.dumps({"type": "s", "data": [json.dumps({"api_version": 1,
                    "state": "idle" if self.helper_idle else "streaming", "owner_present": not self.helper_idle})]})
            raise AssertionError(args)
        if args[0] == "/usr/bin/loginctl":
            if args[1] == "show-user":
                return 0, "c1\n" if self.graphical else "\n"
            return 0, "User=1000\nName=fixture-user\nActive=yes\nRemote=no\nType=wayland\nClass=user\nState=active\nLockedHint=no\n"
        if args[0] == "/usr/bin/systemctl":
            verb = args[1]
            if verb == "show":
                rescue = args[-1] == "panelbridge-rescue.service"
                active, enabled = (self.rescue, False) if rescue else (self.active, self.enabled)
                failed = self.failed and not rescue
                return 0, (f"LoadState=loaded\nActiveState={'failed' if failed else 'active' if active else 'inactive'}\n"
                           f"SubState={'failed' if failed else 'running' if active else 'dead'}\n"
                           f"UnitFileState={'static' if rescue else 'enabled' if enabled else 'disabled'}\n"
                           f"FragmentPath=/usr/lib/systemd/system/{args[-1]}\nDropInPaths=\n")
            if self.fail == verb:
                self.fail = None
                return 1, "PRIVATE COMMAND ERROR"
            if verb == "enable":
                self.enabled = True
            elif verb == "start":
                self.active = True
            elif verb == "stop":
                self.active = False
            elif verb == "disable":
                self.enabled = False
            elif verb == "reset-failed":
                self.failed = False
            elif verb != "daemon-reload":
                raise AssertionError(args)
            if self.crash == verb:
                self.crash = None
                raise Crash(verb)
            return 0, ""
        if args[0] == "/usr/bin/ps":
            return (0, "999 1000 /usr/lib/panelbridge/bin/panelbridge-wfd-worker\n" if self.media else "")
        if args[0] == "/usr/bin/busctl" and "ReloadConfig" in args:
            return 0, ""
        if args[0] == "/usr/bin/busctl" and "ListSessions" in args:
            sessions = [["c1", 1000, "fixture-user", "seat0", "/org/freedesktop/login1/session/c1"]] if self.graphical else []
            return 0, json.dumps({"type": "a(susso)", "data": [sessions]})
        raise AssertionError(args)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    assert importlib.util.find_spec("packaging_tools.maintenance") is not None
    module = importlib.import_module("packaging_tools.maintenance")
    root = tmp_path / "root"
    root.mkdir(mode=0o755)
    root.chmod(0o755)
    for relative in ("etc/panelbridge", "etc/systemd/system", "etc/dbus-1/system.d",
                     "var/lib", "usr/lib/panelbridge/packaging_tools", "usr/lib/systemd/system",
                     "run/user/1000", "home/fixture-user"):
        path = root
        for part in Path(relative).parts:
            path /= part
            path.mkdir(exist_ok=True, parents=True)
            path.chmod(0o755)
    (root / "run/user/1000").chmod(0o700)
    (root / "home/fixture-user").chmod(0o700)
    for relative in ("maintain-user-launch.py", "packaging_tools/__init__.py",
                     "packaging_tools/user_maintenance.py", "packaging_tools/user_setup.py",
                     "packaging_tools/desktop_setup.py", "packaging_tools/runtime_access.py"):
        path = root / "usr/lib/panelbridge" / relative
        path.write_bytes(b"# installed fixture\n")
        path.chmod(0o644)
    system = System()
    system.home = str(root / "home/fixture-user")
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="fixture-user", pw_dir=system.home)
    monkeypatch.setattr(module.pwd, "getpwuid", lambda uid: account)
    monkeypatch.setattr(module.pwd, "getpwnam", lambda name: account)
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setenv("SUDO_GID", "1000")
    monkeypatch.setenv("SUDO_USER", "fixture-user")
    monkeypatch.setenv("HOME", "/not-authority")
    monkeypatch.setenv("PYTHONPATH", "/not-authority")
    lifecycle = module.Maintenance._for_test(root, system)
    monkeypatch.setattr(lifecycle, "_runtime", lambda account, required: {
        "XDG_RUNTIME_DIR": "/run/user/1000", "WAYLAND_DISPLAY": "wayland-0",
        "XDG_SESSION_TYPE": "wayland", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus"})
    return module, root, system, lifecycle


def record(root):
    return json.loads((root / "var/lib/panelbridge/maintenance.json").read_bytes())


def test_fresh_configure_enrolls_then_prepares_user_then_activates(fixture):
    module, root, system, lifecycle = fixture
    result = lifecycle.configure()
    assert result["state"] == "awaiting_desktop_restart"
    assert result["live_output_restored"] is None
    assert json.loads((root / "etc/panelbridge/enrollment.json").read_bytes()) == {"api_version": 1, "normal_uid": 1000}
    assert system.configured and system.enabled and system.active
    saved = record(root)
    assert saved["schema_version"] == 1 and saved["normal_uid"] == 1000
    assert saved["initial_helper"] == {"enabled": False, "active": False}
    assert (root / "var/lib/panelbridge/maintenance.json").stat().st_mode & 0o777 == 0o600
    operations = [args for args, env in system.calls]
    user_index = next(i for i, args in enumerate(operations) if "/usr/lib/panelbridge/maintain-user-launch.py" in args)
    start_index = next(i for i, args in enumerate(operations) if args[:2] == ("/usr/bin/systemctl", "start"))
    assert user_index < start_index
    assert not any("panelbridge-rescue.service" in args and args[1] in ("start", "enable") for args in operations)
    assert "1000" not in json.dumps(result) and system.home not in json.dumps(result)


def test_status_and_never_configured_removal_create_nothing(fixture):
    _, root, system, lifecycle = fixture
    before = sorted(str(p.relative_to(root)) for p in root.rglob("*"))
    assert lifecycle.status()["state"] == "not_configured"
    assert lifecycle.prepare_remove()["state"] == "not_configured"
    assert sorted(str(p.relative_to(root)) for p in root.rglob("*")) == before
    assert not system.configured


def test_removal_waits_for_external_session_transition_before_root_cleanup(fixture, monkeypatch):
    _, root, system, lifecycle = fixture
    lifecycle.configure()
    monkeypatch.delenv("SUDO_UID")
    monkeypatch.delenv("SUDO_GID")
    monkeypatch.delenv("SUDO_USER")
    result = lifecycle.prepare_remove()
    assert result["state"] == "pending_desktop_transition"
    assert not system.configured and system.active and system.enabled
    assert (root / "etc/panelbridge/enrollment.json").exists()
    system.graphical = False
    assert lifecycle.prepare_remove()["state"] == "remove_ready"
    assert not system.active and not system.enabled
    assert not (root / "etc/panelbridge/enrollment.json").exists()
    assert (root / "var/lib/panelbridge/maintenance.json").exists()
    assert lifecycle.prepare_remove()["state"] == "remove_ready"


@pytest.mark.parametrize("field", ["rescue", "session_owner", "media"])
def test_active_owners_block_removal_without_killing_or_user_changes(fixture, field):
    module, root, system, lifecycle = fixture
    lifecycle.configure()
    setattr(system, field, True)
    before = (root / "etc/panelbridge/enrollment.json").read_bytes()
    with pytest.raises(module.MaintenanceError):
        lifecycle.prepare_remove()
    assert system.configured and system.active and system.enabled
    assert (root / "etc/panelbridge/enrollment.json").read_bytes() == before
    assert not any(args[0].endswith("kill") for args, _ in system.calls)


@pytest.mark.parametrize("verb", ["daemon-reload", "enable", "start"])
def test_activation_failure_rolls_back_own_deltas(fixture, verb):
    module, root, system, lifecycle = fixture
    system.fail = verb
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert not system.configured and not system.enabled and not system.active
    assert not (root / "etc/panelbridge/enrollment.json").exists()
    assert record(root)["phase"] == "rolled_back"


@pytest.mark.parametrize("verb", ["enable", "start"])
def test_crash_after_service_mutation_resumes_exact_journal_owned_state(fixture, verb):
    _, root, system, lifecycle = fixture
    system.crash = verb
    with pytest.raises(Crash):
        lifecycle.configure()
    assert record(root)["operations"]["helper_" + verb] == "pending"
    assert lifecycle.configure()["state"] == "awaiting_desktop_restart"
    assert system.configured and system.active and system.enabled


@pytest.mark.parametrize("target", ["etc/panelbridge/enrollment.json", "etc/panelbridge/rescue.json",
    "etc/systemd/system/panelbridge-helper.service", "etc/systemd/system/panelbridge-rescue.service",
    "etc/dbus-1/system.d/org.panelbridge.Helper1.conf"])
def test_unknown_staging_or_enrollment_is_not_adopted(fixture, target):
    module, root, system, lifecycle = fixture
    path = root / target
    path.write_bytes(b"unexplained")
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert path.read_bytes() == b"unexplained"
    assert not (root / "var/lib/panelbridge/maintenance.json").exists()
    assert not system.configured


@pytest.mark.parametrize("key,value", [("SUDO_UID", "0"), ("SUDO_UID", "999"), ("SUDO_UID", "1001"),
    ("SUDO_UID", "1000x"), ("SUDO_GID", "0"), ("SUDO_USER", "somebody-else")])
def test_sudo_identity_must_match_real_account_database(fixture, monkeypatch, key, value):
    module, root, system, lifecycle = fixture
    monkeypatch.setenv(key, value)
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert not system.calls
    assert not (root / "var/lib/panelbridge/maintenance.json").exists()


def test_missing_desktop_refuses_before_enrollment(fixture):
    module, root, system, lifecycle = fixture
    system.graphical = False
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert not (root / "etc/panelbridge/enrollment.json").exists()
    assert not (root / "var/lib/panelbridge/maintenance.json").exists()


@pytest.mark.parametrize("field", ["active", "enabled"])
def test_unowned_initial_helper_state_is_refused(fixture, field):
    module, root, system, lifecycle = fixture
    setattr(system, field, True)
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert not (root / "var/lib/panelbridge/maintenance.json").exists()


def test_user_restore_conflict_retains_root_enrollment_and_service(fixture):
    module, root, system, lifecycle = fixture
    lifecycle.configure()
    system.user_conflict = True
    with pytest.raises(module.MaintenanceError):
        lifecycle.prepare_remove()
    assert system.active and system.enabled
    assert (root / "etc/panelbridge/enrollment.json").exists()


def test_changed_enrollment_is_preserved_on_removal(fixture):
    module, root, system, lifecycle = fixture
    lifecycle.configure()
    path = root / "etc/panelbridge/enrollment.json"
    path.write_bytes(b'{"api_version":1,"normal_uid":2000}')
    with pytest.raises(module.MaintenanceError):
        lifecycle.prepare_remove()
    assert path.read_bytes() == b'{"api_version":1,"normal_uid":2000}'
    assert system.active and system.enabled


def test_read_only_status_rejects_malformed_or_symlink_journal(fixture):
    module, root, system, lifecycle = fixture
    lifecycle.configure()
    path = root / "var/lib/panelbridge/maintenance.json"
    path.write_bytes(b'{"schema_version":2}')
    with pytest.raises(module.MaintenanceError):
        lifecycle.status()
    assert path.read_bytes() == b'{"schema_version":2}'
    other = root / "untouched"
    other.write_bytes(b"private")
    path.unlink()
    path.symlink_to(other)
    with pytest.raises(module.MaintenanceError):
        lifecycle.status()
    assert other.read_bytes() == b"private"


def test_production_constructor_and_cli_refuse_nonroot_and_arbitrary_options(fixture, capsys):
    module, root, system, lifecycle = fixture
    assert os.getuid() != 0
    with pytest.raises(module.MaintenanceError):
        module.Maintenance()
    for argv in (["status"], ["configure", "--root", "/private/fixture"], ["anything"]):
        assert module.main(argv) != 0
        output = capsys.readouterr().out
        assert "fixture" not in output
        assert json.loads(output)["live_output_restored"] is None


def test_private_test_root_cannot_point_to_real_root(fixture):
    module, _, system, _ = fixture
    with pytest.raises(module.MaintenanceError):
        module.Maintenance._for_test("/", system)


def test_logged_out_user_is_detected_without_requesting_an_absent_logind_user(fixture):
    _, _, system, lifecycle = fixture
    lifecycle.configure()
    system.graphical = False
    assert lifecycle.prepare_remove()["state"] == "remove_ready"
    assert not any(args[:2] == ("/usr/bin/loginctl", "show-user") for args, _ in system.calls)


def test_malformed_unprivileged_receipt_cannot_escape_as_traceback(fixture):
    module, _, system, lifecycle = fixture
    original = lifecycle._as_user
    def malformed(account, args, runtime):
        if "/usr/lib/panelbridge/maintain-user-launch.py" in args:
            return 0, json.dumps({"api_version": 1, "action": args[-1], "state": [],
                                 "scope": "configuration_bytes_only", "live_output_restored": None})
        return original(account, args, runtime)
    lifecycle._as_user = malformed
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert not system.active


def test_unrelated_process_argument_quotes_do_not_break_idle_inspection(fixture):
    _, _, system, lifecycle = fixture
    run = lifecycle.run
    def quoted(args, **kwargs):
        if args[0] == "/usr/bin/ps":
            return 0, "42 1000 /usr/bin/editor don't parse this as shell\n"
        return run(args, **kwargs)
    lifecycle.run = quoted
    assert lifecycle.configure()["state"] == "awaiting_desktop_restart"


@pytest.mark.parametrize("damage", [
    lambda state: state["initial_helper"].update(active=0),
    lambda state: state["operations"].update(helper_start="not_started"),
    lambda state: state.update(user_setup=None),
    lambda state: state.update(user_setup={"state": [], "scope": "configuration_bytes_only", "live_output_restored": None}),
])
def test_inconsistent_completed_journal_is_not_reported_as_success(fixture, damage):
    module, root, _, lifecycle = fixture
    lifecycle.configure()
    state = record(root)
    damage(state)
    path = root / "var/lib/panelbridge/maintenance.json"
    path.write_text(json.dumps(state))
    with pytest.raises(module.MaintenanceError):
        lifecycle.status()


def test_completed_removal_refuses_reappearing_enrollment(fixture):
    module, root, system, lifecycle = fixture
    lifecycle.configure()
    path = root / "etc/panelbridge/enrollment.json"
    before = path.read_bytes()
    system.graphical = False
    lifecycle.prepare_remove()
    path.write_bytes(before)
    path.chmod(0o600)
    with pytest.raises(module.MaintenanceError):
        lifecycle.prepare_remove()
    assert path.read_bytes() == before


@pytest.mark.parametrize("action,operation", [
    ("configure", "enrollment"), ("configure", "user_configure"), ("configure", "reload"),
    ("configure", "helper_enable"), ("configure", "helper_start"),
    ("prepare_remove", "user_restore"), ("prepare_remove", "helper_stop"),
    ("prepare_remove", "helper_disable"), ("prepare_remove", "enrollment_restore"),
])
@pytest.mark.parametrize("after", [False, True])
def test_every_effect_boundary_can_resume_after_process_death(fixture, action, operation, after):
    _, root, system, lifecycle = fixture
    if action == "prepare_remove":
        lifecycle.configure()
        system.graphical = False
    original = lifecycle._step
    def interrupted(name, effect):
        if name != operation:
            return original(name, effect)
        def crash():
            if after:
                effect()
            raise Crash(name)
        return original(name, crash)
    lifecycle._step = interrupted
    with pytest.raises(Crash):
        getattr(lifecycle, action)()
    assert record(root)["operations"][operation] == "pending"
    lifecycle._step = original
    expected = "awaiting_desktop_restart" if action == "configure" else "remove_ready"
    assert getattr(lifecycle, action)()["state"] == expected


@pytest.mark.parametrize("script,reason", [
    ("import os; os.write(1, b'x'*262145)", "command_output_too_large"),
    ("import time; time.sleep(10)", "command_timeout"),
])
def test_bounded_runner_kills_only_its_own_process_group(fixture, script, reason):
    module, _, _, _ = fixture
    start = time.monotonic()
    with pytest.raises(module.MaintenanceError, match=reason):
        module._run([sys.executable, "-I", "-B", "-c", script], env={"LC_ALL": "C"}, timeout=0.2)
    assert time.monotonic() - start < 2


@pytest.mark.parametrize("failure", ["timeout", "overflow"])
def test_runner_cleans_descendant_after_leader_exits(tmp_path, monkeypatch, failure):
    module = importlib.import_module("packaging_tools.maintenance")
    fifo = tmp_path / "descendant-lifetime"
    os.mkfifo(fifo, 0o600)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    launched = []
    descendant_stopped = False
    real_popen = module.subprocess.Popen

    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", launch)
    # Only the descendant opens the FIFO's writer. EOF proves its descriptor
    # closed even if a system reaper has not yet removed the dead process's PID.
    script = """import os, signal, sys, time
leader = os.getpid()
if os.fork():
    os._exit(0)
while os.getppid() == leader:
    time.sleep(0.001)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
writer = os.open(sys.argv[1], os.O_WRONLY)
os.write(writer, b'ready')
if sys.argv[2] == 'overflow':
    os.write(1, b'x' * 262145)
time.sleep(10)
"""
    try:
        reason = "command_timeout" if failure == "timeout" else "command_output_too_large"
        start = time.monotonic()
        with pytest.raises(module.MaintenanceError, match=reason):
            module._run([sys.executable, "-I", "-B", "-c", script, str(fifo), failure],
                        env={"LC_ALL": "C"}, timeout=0.3)
        assert time.monotonic() - start < 2
        assert os.read(reader, 5) == b"ready"
        deadline = time.monotonic() + 1
        while True:
            try:
                assert os.read(reader, 1) == b""
                descendant_stopped = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail("Owned descendant remains alive after command failure")
                time.sleep(0.01)
    finally:
        for process in launched:
            if not descendant_stopped:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=1)
        os.close(reader)


def test_runner_returns_exit_without_echoing_stderr(fixture):
    module, _, _, _ = fixture
    result = module._run([sys.executable, "-I", "-B", "-c",
        "import sys; sys.stdout.write('receipt'); sys.stderr.write('PRIVATE'); sys.exit(3)"], env={"LC_ALL": "C"})
    assert result == (3, "receipt")


def test_cli_pending_removal_is_nonzero_with_frozen_json(fixture, monkeypatch, capsys):
    module, _, _, lifecycle = fixture
    lifecycle.configure()
    monkeypatch.setattr(module, "Maintenance", lambda: lifecycle)
    assert module.main(["prepare-remove"]) == 4
    assert json.loads(capsys.readouterr().out) == {"api_version": 1, "action": "prepare-remove",
        "state": "pending_desktop_transition", "scope": "configuration_bytes_only", "live_output_restored": None}


def test_installed_launcher_refuses_nonroot_even_with_isolation(fixture):
    launcher = Path(__file__).resolve().parents[1] / "packaging/maintain-launch.py"
    result = subprocess.run([sys.executable, "-I", "-B", str(launcher), "status"], capture_output=True, text=True)
    assert result.returncode != 0 and "effective root" in result.stderr


def test_owned_failed_start_rolls_back_after_idle_network_proof(fixture):
    module, root, system, lifecycle = fixture
    original = lifecycle.run
    def fails_started_unit(args, **kwargs):
        if args[:2] == ["/usr/bin/systemctl", "start"]:
            system.failed = True
            return 1, "start failed"
        return original(args, **kwargs)
    lifecycle.run = fails_started_unit
    with pytest.raises(module.MaintenanceError, match="configuration_failed_rolled_back"):
        lifecycle.configure()
    assert not system.failed and not system.active and not system.enabled
    assert not (root / "etc/panelbridge/enrollment.json").exists()
    assert record(root)["phase"] == "rolled_back"


def test_unowned_failed_helper_is_not_reset(fixture):
    module, root, system, lifecycle = fixture
    system.failed = True
    with pytest.raises(module.MaintenanceError):
        lifecycle.configure()
    assert system.failed
    assert not (root / "var/lib/panelbridge/maintenance.json").exists()


def test_real_runtime_validation_rejects_regular_socket_impostor(fixture):
    module, root, _, lifecycle = fixture
    account = lifecycle._account(1000)
    path = root / "run/user/1000/wayland-0"
    path.write_bytes(b"not a socket")
    with pytest.raises(module.MaintenanceError, match="unsafe_user_socket"):
        module.Maintenance._runtime(lifecycle, account, True)
    path.unlink()
    assert module.Maintenance._runtime(lifecycle, account, False) == {"XDG_RUNTIME_DIR": "/run/user/1000"}
    with pytest.raises(module.MaintenanceError, match="one_wayland_socket_required"):
        module.Maintenance._runtime(lifecycle, account, True)
    (root / "run/user/1000").rmdir()
    assert module.Maintenance._runtime(lifecycle, account, False) == {}


def test_runtime_requires_owned_private_directory_and_exact_socket_selection(fixture, monkeypatch):
    module, root, _, lifecycle = fixture
    account = lifecycle._account(1000)
    runtime = root / "run/user/1000"
    (runtime / "wayland-0").write_bytes(b"fixture endpoint")
    original = os.stat
    def socket_info(path, *args, **kwargs):
        if path in ("wayland-0", "wayland-1"):
            assert kwargs["follow_symlinks"] is False
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o700, st_uid=os.getuid())
        return original(path, *args, **kwargs)
    monkeypatch.setattr(module.os, "stat", socket_info)
    assert module.Maintenance._runtime(lifecycle, account, True)["WAYLAND_DISPLAY"] == "wayland-0"
    (runtime / "wayland-1").write_bytes(b"second endpoint")
    with pytest.raises(module.MaintenanceError, match="one_wayland_socket_required"):
        module.Maintenance._runtime(lifecycle, account, True)
    runtime.chmod(0o777)
    with pytest.raises(module.MaintenanceError, match="unsafe_user_runtime"):
        module.Maintenance._runtime(lifecycle, account, True)


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


def test_runuser_boundary_revalidates_runtime_before_every_call(fixture, monkeypatch):
    module, _, system, lifecycle = fixture
    account = lifecycle._account(1000)
    runtime = lifecycle._runtime(account, True)
    checks = []
    original = lifecycle._runtime
    def check(account, required):
        checks.append(required)
        return original(account, required)
    monkeypatch.setattr(lifecycle, '_runtime', check)
    lifecycle._user(account, 'status', runtime)
    lifecycle._user(account, 'status', runtime)
    assert checks == [True, True]
    monkeypatch.setattr(lifecycle, '_runtime', lambda account, required: {})
    def forbidden(*args, **kwargs):
        pytest.fail('runuser reached after runtime changed')
    monkeypatch.setattr(lifecycle, 'run', forbidden)
    with pytest.raises(module.MaintenanceError, match='user_runtime_changed'):
        lifecycle._as_user(account, ['/usr/bin/busctl', '--user'], runtime)
