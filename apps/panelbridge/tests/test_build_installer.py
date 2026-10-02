"""Synthetic archives and fake command boundaries only; never sudo or APT."""

import hashlib
import builtins
from importlib.machinery import SourceFileLoader
import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tarfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from packaging_tools import build_package as package
from packaging_tools import source_bundle as source
from packaging_tools.build_installer import InstallerError, build_installer

APP = Path(__file__).resolve().parents[1]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def archive(payload):
    members = {"debian-binary": b"2.0\n"}
    for section in ("control", "data"):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as output:
            for name, (data, mode) in sorted(payload.items()):
                control = name.startswith("DEBIAN/")
                if control != (section == "control"):
                    continue
                info = tarfile.TarInfo(name.removeprefix("DEBIAN/") if control else name)
                info.size, info.mode, info.uid, info.gid, info.mtime = len(data), mode, 0, 0, 0
                output.addfile(info, io.BytesIO(data))
        members[section + ".tar.gz"] = stream.getvalue()
    result = b"!<arch>\n"
    for name, data in members.items():
        result += f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode()
        result += data + (b"\n" if len(data) % 2 else b"")
    return result


@pytest.fixture
def inputs(tmp_path):
    project = tmp_path / "project"
    for name in source.SOURCE_FILES:
        target = project / "apps/panelbridge" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((APP / name).read_bytes())
    bundle = source.build_bundle(project, "source").path
    worker = project / "worker"
    ident = b"\x7fELF\x02\x01\x01" + b"\0" * 9
    header = struct.pack("<HHIQQQIHHHHHH", 3, 183, 1, 0x1000, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    segment = struct.pack("<IIQQQQQQ", 1, 5, 120, 0x1000, 0x1000, 4, 4, 4)
    worker.write_bytes(ident + header + segment + b"\0" * 4)
    built = package.build_package(project, "package", source_bundle=bundle, worker=worker, build_deb=False)
    payload = {str(p.relative_to(built.payload_dir)): (p.read_bytes(), p.stat().st_mode & 0o777)
               for p in built.payload_dir.rglob("*") if p.is_file()}
    deb = project / "panelbridge.deb"
    deb.write_bytes(archive(payload))
    receipt = json.loads(built.receipt_path.read_bytes())
    receipt.update(deb_status="built", deb_sha256=digest(deb.read_bytes()), ready_for_install=True)
    built.receipt_path.write_bytes(source._json(receipt))
    return project, deb, bundle, built.receipt_path


def build(inputs, destination="installer"):
    project, deb, bundle, receipt = inputs
    return build_installer(project, destination, deb=deb, source_bundle=bundle, package_receipt=receipt)


def test_single_executable_is_deterministic_and_experimental(inputs):
    first, second = build(inputs, "one"), build(inputs, "two")
    assert first.path.read_bytes() == second.path.read_bytes()
    assert first.sha256 == digest(first.path.read_bytes())
    assert first.path.stat().st_mode & 0o777 == 0o755
    assert first.ready_for_install is True
    assert first.lifecycle_inputs_present is True
    assert first.path.name == f"panelbridge_{package.VERSION}_arm64_experimental.run"
    assert b"EXPERIMENTAL_INSTALL_ENABLED = True" in first.path.read_bytes()
    assert str(inputs[0]).encode() not in first.path.read_bytes()


def test_enabled_artifact_validates_bundle_and_reaches_only_mock_sudo(inputs, monkeypatch):
    artifact = build(inputs).path
    loader = SourceFileLoader("panelbridge_enabled_artifact_test", str(artifact))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    decoded, receipt, snapshot = module._bundle()
    assert receipt["version"] == package.VERSION
    assert receipt["ready_for_install"] is True
    assert receipt["release_ready"] is False
    assert module._lifecycle_present(receipt, snapshot)
    assert digest(decoded["package.deb"]) == receipt["deb_sha256"]
    calls = []
    monkeypatch.setattr(module, "_normal_account", lambda uid: SimpleNamespace(pw_uid=uid))
    monkeypatch.setattr(module.subprocess, "call", lambda args, **kw: calls.append((args, kw)) or 17)
    assert module.main([]) == 17
    assert len(calls) == 1
    args, options = calls[0]
    assert args[:4] == ["/usr/bin/sudo", "/usr/bin/python3", "-I", "-B"]
    assert args[-2] == str(artifact)
    assert args[-1] == digest(artifact.read_bytes())
    assert options["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize("which", [1, 2, 3])
def test_corrupt_input_is_rejected_before_output(inputs, which):
    inputs[which].write_bytes(inputs[which].read_bytes() + b"tampered")
    with pytest.raises(InstallerError):
        build(inputs)
    assert not (inputs[0] / "installer").exists()


def test_stale_source_and_existing_output_are_preserved(inputs):
    first = build(inputs)
    before = first.path.read_bytes()
    with pytest.raises(InstallerError):
        build(inputs)
    assert first.path.read_bytes() == before
    (inputs[0] / "apps/panelbridge/panelbridge/models.py").write_text("changed\n")
    with pytest.raises(InstallerError):
        build(inputs, "new-output")
    assert not (inputs[0] / "new-output").exists()


def test_output_symlink_escape_is_rejected(inputs, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (inputs[0] / "installer").symlink_to(outside)
    with pytest.raises(InstallerError):
        build(inputs)
    assert list(outside.iterdir()) == []


@pytest.fixture
def bootstrap():
    spec = importlib.util.spec_from_file_location("installer_bootstrap_test", APP / "packaging/installer-bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_unfinished_gate_precedes_every_privileged_boundary(bootstrap, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "EXPERIMENTAL_INSTALL_ENABLED", False)
    monkeypatch.setattr(bootstrap, "_bundle", lambda: ({}, {}, {}))
    monkeypatch.setattr(bootstrap, "_lifecycle_present", lambda *args: True)
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda *args, **kw: pytest.fail("sudo must remain disabled"))
    monkeypatch.setattr(bootstrap, "_root_main", lambda *args: pytest.fail("root must remain disabled"))
    for args in ([], ["--root-staged"]):
        assert bootstrap.main(args) == 3
        assert json.loads(capsys.readouterr().out)["state"] == "developer_unfinished"
    for args in (["--force"], ["--ready"], ["--root", "/tmp/anywhere"]):
        assert bootstrap.main(args) == 2
        assert json.loads(capsys.readouterr().out)["state"] == "installer_refused"


@pytest.mark.parametrize("data,deb822", [
    (b"deb [trusted=yes] https://example.invalid stable main", False),
    (b"deb [allow-insecure=true] https://example.invalid stable main", False),
    (b"Types: deb\nTrusted: yes\n", True),
    (b"Types: deb\nAllow-Weak: true\n", True),
])
def test_repository_authentication_overrides_are_refused(bootstrap, data, deb822):
    with pytest.raises(bootstrap.InstallRefused, match="repository_authentication_override"):
        bootstrap._source_security(data, deb822)


@pytest.mark.parametrize("data,deb822", [
    (b"deb [signed-by=/usr/share/keyrings/debian.gpg] https://example.invalid stable main", False),
    (b"deb [trusted=no allow-insecure=false] https://example.invalid stable main", False),
    (b"Types: deb\nTrusted: no\nSigned-By: /usr/share/keyrings/debian.gpg\n", True),
])
def test_source_authentication_guards_ignore_uri_and_key_contents(bootstrap, data, deb822):
    assert bootstrap._source_security(data, deb822) is None


def test_custom_apt_root_is_refused_before_any_source_reads(bootstrap, monkeypatch):
    def config(args, environment, **options):
        return (b"ROOT='/other'\n" if "ROOT" in args else b"") + b"ETC='etc/apt'\nLIST='sources.list'\nPARTS='sources.list.d'\n"
    monkeypatch.setattr(bootstrap, "_checked", config)
    monkeypatch.setattr(bootstrap, "_read_system", lambda *args: pytest.fail("No reads from custom roots"))
    with pytest.raises(bootstrap.InstallRefused):
        bootstrap._repositories({})


@pytest.mark.parametrize("uid,gid,user", [("0", "1000", "builder"), ("1000", "0", "builder"),
                                         ("1000", "1000", "other"), ("bad", "1000", "builder")])
def test_sudo_identity_is_checked_against_passwd(bootstrap, monkeypatch, uid, gid, user):
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 0)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.os, "environ", {"SUDO_UID": uid, "SUDO_GID": gid, "SUDO_USER": user})
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="builder"))
    with pytest.raises(bootstrap.InstallRefused):
        bootstrap._sudo_account()


def test_system_account_is_refused_before_any_home_or_command_access(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 0)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 0)
    monkeypatch.setattr(bootstrap.os, "environ", {"SUDO_UID": "500", "SUDO_GID": "500", "SUDO_USER": "builder"})
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda uid: SimpleNamespace(pw_uid=500, pw_gid=500, pw_name="builder"))
    with pytest.raises(bootstrap.InstallRefused):
        bootstrap._sudo_account()


@pytest.mark.parametrize("leader_exited", [True, False])
def test_timeout_kills_owned_group_even_after_leader_exits(bootstrap, monkeypatch, leader_exited):
    signals = []
    clock = iter(value / 10 for value in range(1000))
    stream = SimpleNamespace(fileno=lambda: 12, close=lambda: None)
    process = SimpleNamespace(pid=12345, stdout=stream, stderr=stream,
                              poll=lambda: 0 if leader_exited or signals else None,
                              wait=lambda **kw: 0)
    class Selector:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def register(self, *args):
            pass
        def get_map(self):
            return {12: "descendant holds pipe"}
        def select(self, *args):
            return []
    def killpg(group, sig):
        assert group == process.pid
        signals.append(sig)
        if bootstrap.signal.SIGKILL in signals and sig == 0:
            raise ProcessLookupError
    monkeypatch.setattr(bootstrap.subprocess, "Popen", lambda *args, **kw: process)
    monkeypatch.setattr(bootstrap.selectors, "DefaultSelector", Selector)
    monkeypatch.setattr(bootstrap.os, "set_blocking", lambda *args: None)
    monkeypatch.setattr(bootstrap.os, "killpg", killpg)
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(bootstrap.time, "sleep", lambda *args: None)
    with pytest.raises(bootstrap.InstallRefused, match="command_timeout"):
        bootstrap._run(["fake"], {}, timeout=0.2)
    assert bootstrap.signal.SIGTERM in signals
    assert bootstrap.signal.SIGKILL in signals


def test_inventory_rejects_prior_app_and_broken_state_but_accepts_held_packages(bootstrap, monkeypatch):
    rows = b"labwc\tii \t0.8\tarm64\nnetwork-manager\thi \t1.52\tarm64\nsystemd\tii \t257\tarm64\n"
    def command(args, env, **options):
        return b"" if args[1] == "--audit" else rows
    monkeypatch.setattr(bootstrap, "_checked", command)
    assert bootstrap._inventory({}) == {"labwc": "0.8", "network-manager": "1.52", "systemd": "257"}
    for bad in (b"panelbridge\tii \t0.1.0~dev1\tarm64\n", b"panelbridge\trc \t0.1.0~dev1\tarm64\n",
                b"python3-cairo\tiU \t1.27.0-2\tarm64\n"):
        monkeypatch.setattr(bootstrap, "_checked", lambda args, env, **kw: b"" if args[1] == "--audit" else rows + bad)
        with pytest.raises(bootstrap.InstallRefused):
            bootstrap._inventory({})


@pytest.fixture
def engine(bootstrap, monkeypatch):
    from packaging_tools import apt_plan
    calls, results, events = [], [], []
    installed = {name: {"version": version, "status": "ii ", "architecture": "arm64"}
                 for name, version in {"labwc": "0.8", "network-manager": "1.52", "systemd": "257"}.items()}
    for name in ("_host", "_native_locks", "_repositories", "_collisions"):
        monkeypatch.setattr(bootstrap, name, lambda *args: None)
    monkeypatch.setattr(bootstrap, "_package_state", lambda env: json.loads(json.dumps(installed)))
    monkeypatch.setattr(bootstrap, "_write_identical", lambda *args: None)
    monkeypatch.setattr(bootstrap, "_maintenance_binding", lambda *args: None)
    monkeypatch.setattr(bootstrap, "_normal_account", lambda uid: account)
    monkeypatch.setattr(bootstrap, "_save_result", lambda stage, result: results.append(json.loads(json.dumps(result))))
    monkeypatch.setattr(bootstrap, "_apt_module", lambda *args: apt_plan)
    monkeypatch.setattr(bootstrap, "_read_system", lambda path, limit: b"a" * 64)
    monkeypatch.setattr(bootstrap, "_private_read", lambda path, limit: b"a" * 64)
    monkeypatch.setattr(bootstrap, "_verify_installed", lambda receipt: events.append("verified"))
    simulation = b"""0 upgraded, 2 newly installed, 0 to remove and 3 not upgraded.
Inst python3-cairo (1.27.0-2 Debian:13/stable [arm64])
Inst panelbridge (0.1.0~dev1 local-deb [arm64])
Conf python3-cairo (1.27.0-2 Debian:13/stable [arm64])
Conf panelbridge (0.1.0~dev1 local-deb [arm64])
"""
    def run(args, environment, **options):
        calls.append((args, dict(environment)))
        if args[0] == "/usr/bin/apt-get":
            if "--simulate" in args:
                return 0, simulation, b""
            installed.update({name: {"status": "ii ", "version": version, "architecture": "arm64"}
                              for name, version in {"python3-cairo": "1.27.0-2", "panelbridge": "0.1.0~dev1"}.items()})
            return 0, b"", b""
        if args[0] == "/usr/bin/dpkg":
            return 0, b"", b""
        if args[0] == "/usr/bin/dpkg-query":
            return 0, b"install ok installed\t0.1.0~dev1\n", b""
        assert args == ["/usr/bin/python3", "-I", "-B", "/usr/lib/panelbridge/maintain-launch.py", "configure"]
        assert events[-1] == "verified"
        return 0, json.dumps(bootstrap.SUCCESS).encode(), b""
    monkeypatch.setattr(bootstrap, "_run", run)
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="builder", pw_dir="/fixture-home")
    return SimpleNamespace(module=bootstrap, calls=calls, results=results, events=events, run=run,
                           installed=installed, simulation=simulation,
                           account=account)


def execute(engine, resume=None):
    return engine.module._execute(Path("/var/lib/panelbridge/installer-staging/" + "a" * 32),
                                  {"package.deb": b"package", "source.tar.gz": b"source", "package-receipt.json": b"receipt"},
                                  {"version": "0.1.0~dev1"}, {}, engine.account, resume=resume)


def test_dormant_engine_pins_additions_revalidates_and_only_then_configures(engine):
    code, result = execute(engine)
    assert code == 0 and result["state"] == "awaiting_desktop_restart"
    assert result["ready_for_install"] is True
    apt = [args for args, env in engine.calls if args[0] == "/usr/bin/apt-get"]
    assert len(apt) == 3 and ["--simulate" in args for args in apt] == [True, True, False]
    assert "python3-cairo:arm64=1.27.0-2" not in apt[0]
    assert "python3-cairo:arm64=1.27.0-2" in apt[1] and "python3-cairo:arm64=1.27.0-2" in apt[2]
    assert all("--no-remove" in args and "--no-upgrade" in args for args in apt)
    assert all("APT::Get::AllowUnauthenticated=false" in args for args in apt)
    assert all(env["SUDO_UID"] == "1000" and env["SUDO_GID"] == "1000" for args, env in engine.calls)
    assert [r["state"] for r in engine.results] == ["prepared", "package_transaction_prepared",
                                                  "package_transaction_started", "installed_unconfigured",
                                                  "configure_started",
                                                  "awaiting_desktop_restart"]


@pytest.mark.parametrize("failure", ["stderr", "resolution", "changed", "install", "maintenance", "maintenance-shape"])
def test_failed_engine_keeps_honest_receipt_without_claiming_dependency_rollback(engine, monkeypatch, failure):
    simulations = []
    def run(args, env, **options):
        if args[0] == "/usr/bin/apt-get":
            if "--simulate" in args:
                simulations.append(args)
                if failure == "stderr":
                    return 0, engine.simulation, b"PRIVATE WARNING"
                if failure == "resolution":
                    return 100, b"PRIVATE ERROR", b""
                if failure == "changed" and len(simulations) == 2:
                    return 0, engine.simulation.replace(b"1.27.0-2", b"1.27.0-3"), b""
            elif failure == "install":
                return 100, b"PRIVATE ERROR", b""
        if args[0] == "/usr/bin/python3":
            if failure == "maintenance":
                return 3, b"PRIVATE ERROR", b""
            if failure == "maintenance-shape":
                return 0, b'{"state":"configured"}', b""
        return engine.run(args, env, **options)
    monkeypatch.setattr(engine.module, "_run", run)
    code, result = execute(engine)
    assert code == 4 and result["state"] == "installation_incomplete"
    assert result["dependencies_rolled_back"] is False
    assert "PRIVATE" not in json.dumps(engine.results)
    assert engine.results[-1] == result
    if failure in ("stderr", "resolution", "changed"):
        assert not any(args[0] == "/usr/bin/apt-get" and "--simulate" not in args for args, env in engine.calls)


def test_pre_apt_receipt_binds_exact_inputs_and_enrolling_account(engine):
    assert execute(engine)[0] == 0
    first = engine.results[0]
    assert first["binding"]["artifact_sha256"] == "a" * 64
    assert first["binding"]["deb_sha256"] == digest(b"package")
    assert first["binding"]["source_sha256"] == digest(b"source")
    assert first["binding"]["enrolling_uid"] == 1000
    assert first["resume_supported"] is True
    prepared = engine.results[1]
    assert set(prepared["baseline"]) == {"labwc", "network-manager", "systemd"}
    assert {row["package"] for row in prepared["new_packages"]} == {"panelbridge", "python3-cairo"}


def test_completed_owned_install_resumes_maintenance_without_any_apt(engine, monkeypatch):
    def failed_configure(args, env, **options):
        return (4, b"", b"") if args[0] == "/usr/bin/python3" else engine.run(args, env, **options)
    monkeypatch.setattr(engine.module, "_run", failed_configure)
    code, journal = execute(engine)
    assert code == 4 and journal["phase"] == "configure_started"
    engine.calls.clear()
    monkeypatch.setattr(engine.module, "_run", engine.run)
    code, resumed = execute(engine, journal)
    assert code == 0 and resumed["state"] == "awaiting_desktop_restart"
    assert [args[0] for args, env in engine.calls] == ["/usr/bin/python3"]


def test_receipt_rehashed_package_cannot_substitute_installed_source(inputs):
    deb = inputs[1].read_bytes()
    from packaging_tools.build_installer import _deb_files
    payload = _deb_files(deb)
    target = "usr/lib/panelbridge/panelbridge/models.py"
    payload[target] = (b"unreviewed code\n", 0o644)
    changed = archive(payload)
    inputs[1].write_bytes(changed)
    receipt = json.loads(inputs[3].read_bytes())
    receipt["deb_sha256"] = digest(changed)
    for entry in receipt["payload_files"]:
        if entry["path"] == target:
            entry.update(size=len(payload[target][0]), sha256=digest(payload[target][0]))
    inputs[3].write_bytes(source._json(receipt))
    with pytest.raises(InstallerError):
        build(inputs)
    assert not (inputs[0] / "installer").exists()


@pytest.mark.parametrize("field,value", [("version", "9.9"), ("architecture", "amd64"),
                                         ("source_snapshot_matches", False), ("ready_for_install", False),
                                         ("release_ready", True)])
def test_receipt_identity_and_readiness_claims_cannot_override_builder(inputs, field, value):
    receipt = json.loads(inputs[3].read_bytes())
    receipt[field] = value
    inputs[3].write_bytes(source._json(receipt))
    with pytest.raises(InstallerError):
        build(inputs)


@pytest.mark.parametrize("kind", ["symlink", "hardlink"])
def test_input_links_are_refused_before_output(inputs, tmp_path, kind):
    original = inputs[1]
    replacement = tmp_path / "copy.deb"
    replacement.write_bytes(original.read_bytes())
    original.unlink()
    if kind == "symlink":
        original.symlink_to(replacement)
    else:
        os.link(replacement, original)
    with pytest.raises(InstallerError):
        build(inputs)
    assert not (inputs[0] / "installer").exists()


def test_traversal_is_reported_as_the_public_builder_error(inputs):
    with pytest.raises(InstallerError):
        build_installer(inputs[0] / ".." / "project", "out", deb=inputs[1],
                        source_bundle=inputs[2], package_receipt=inputs[3])


@pytest.mark.parametrize("mutation", ["none", "hash", "symlink", "identity", "system-uid", "root-gid",
                                     "name-mismatch", "home-symlink", "home-writable", "home-owner"])
def test_real_loader_code_copies_once_into_trusted_fake_root_and_never_executes_user_path(
        bootstrap, tmp_path, capsys, mutation):
    # Run the exact constant loader with only OS/account/exec boundaries replaced.
    # All real I/O is inside this test directory; no sudo or root operation runs.
    root = tmp_path / "root"
    (root / "var/lib").mkdir(parents=True)
    (root / "fixture-home").mkdir(parents=True)
    home_inode = (root / "fixture-home").stat().st_ino
    if mutation == "home-symlink":
        (root / "fixture-link").symlink_to(root / "fixture-home")
    if mutation == "home-writable":
        (root / "fixture-home").chmod(0o777)
    artifact = tmp_path / "developer.run"
    artifact.write_bytes(b"reviewed bootstrap bytes\n")
    source_inode = artifact.stat().st_ino
    expected = digest(artifact.read_bytes()) if mutation != "hash" else "0" * 64
    supplied = artifact
    if mutation == "symlink":
        supplied = tmp_path / "symlink.run"
        supplied.symlink_to(artifact)
    calls, opened, descriptors = [], [], set()
    class Executed(BaseException):
        pass
    class FakeOS:
        environ = {"SUDO_UID": "1000", "SUDO_GID": "1000", "SUDO_USER": "wrong" if mutation == "identity" else "builder"}
        if mutation == "system-uid":
            environ.update(SUDO_UID="500")
        if mutation == "root-gid":
            environ.update(SUDO_GID="0")
        def __getattr__(self, name):
            return getattr(os, name)
        def getuid(self):
            return 0
        def geteuid(self):
            return 0
        def open(self, path, flags, *args, **kwargs):
            opened.append(str(path))
            fd = os.open(root if path == "/" else path, flags, *args, **kwargs)
            descriptors.add(fd)
            return fd
        def close(self, fd):
            descriptors.discard(fd)
            os.close(fd)
        def fstat(self, fd):
            info = os.fstat(fd)
            fields = {key: getattr(info, key) for key in ("st_mode", "st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}
            owner = 1000 if info.st_ino in (source_inode, home_inode) else 0
            return SimpleNamespace(**fields, st_uid=0 if info.st_ino == home_inode and mutation == "home-owner" else owner, st_gid=0)
        def execve(self, executable, args, env):
            calls.append((executable, args, env))
            raise Executed()
    fake_os = FakeOS()
    fake_sys = SimpleNamespace(argv=["-c", str(supplied), expected], exit=lambda code: (_ for _ in ()).throw(SystemExit(code)))
    account = SimpleNamespace(pw_uid=500 if mutation == "system-uid" else 1000,
                              pw_gid=0 if mutation == "root-gid" else 1000, pw_name="builder",
                              pw_dir="/fixture-link" if mutation == "home-symlink" else "/fixture-home")
    named = SimpleNamespace(**vars(account))
    if mutation == "name-mismatch":
        named.pw_gid = 2000
    fake_pwd = SimpleNamespace(getpwuid=lambda uid: account, getpwnam=lambda name: named)
    real_import = builtins.__import__
    def fake_import(name, *args, **kwargs):
        return {"os": fake_os, "sys": fake_sys, "pwd": fake_pwd}.get(name) or real_import(name, *args, **kwargs)
    try:
        with pytest.raises(Executed if mutation == "none" else SystemExit):
            exec(bootstrap.ROOT_LOADER, {"__builtins__": {**vars(builtins), "__import__": fake_import}})
    finally:
        for fd in descriptors:
            try:
                os.close(fd)
            except OSError:
                pass
    if mutation == "none":
        assert len(calls) == 1
        executable, args, env = calls[0]
        assert executable == "/usr/bin/python3" and args[1:3] == ["-I", "-B"]
        staged = root / args[3].lstrip("/")
        assert staged.read_bytes() == artifact.read_bytes()
        assert staged.parent.stat().st_mode & 0o777 == 0o700
        assert staged.stat().st_mode & 0o777 == 0o600
        assert opened.count(str(artifact)) == 1
        assert env["SUDO_UID"] == "1000" and env["SUDO_USER"] == "builder"
        assert str(artifact) not in args
    else:
        assert not calls and not (root / "var/lib/panelbridge").exists()
        assert json.loads(capsys.readouterr().out)["state"] == "staging_refused"


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize("phase", ["prepared", "package_transaction_prepared", "package_transaction_started",
                                   "installed_unconfigured", "configure_started", "after_configure_success"])
def test_retry_reconciles_each_durable_crash_boundary(engine, monkeypatch, phase):
    save = engine.module._save_result
    def crash(stage, result):
        if phase == "after_configure_success" and result["phase"] == "awaiting_desktop_restart":
            raise SimulatedCrash
        save(stage, result)
        if result["phase"] == phase:
            raise SimulatedCrash
    monkeypatch.setattr(engine.module, "_save_result", crash)
    with pytest.raises(SimulatedCrash):
        execute(engine)
    previous = engine.results[-1]
    engine.calls.clear()
    monkeypatch.setattr(engine.module, "_save_result", save)
    code, result = execute(engine, previous)
    apt = [args for args, env in engine.calls if args[0] == "/usr/bin/apt-get"]
    if phase == "package_transaction_started":
        assert code == 4 and result["reason"] == "interrupted_package_transaction_requires_review"
        assert not engine.calls
    elif phase in ("prepared", "package_transaction_prepared"):
        assert code == 0 and len(apt) == 3
    else:
        assert code == 0 and not apt
        assert [args[0] for args, env in engine.calls] == ["/usr/bin/python3"]


def test_crash_after_apt_before_receipt_continues_only_maintenance(engine, monkeypatch):
    def crash(args, env, **options):
        value = engine.run(args, env, **options)
        if args[0] == "/usr/bin/apt-get" and "--simulate" not in args:
            raise SimulatedCrash
        return value
    monkeypatch.setattr(engine.module, "_run", crash)
    with pytest.raises(SimulatedCrash):
        execute(engine)
    assert engine.results[-1]["phase"] == "package_transaction_started"
    engine.calls.clear()
    monkeypatch.setattr(engine.module, "_run", engine.run)
    assert execute(engine, engine.results[-1])[0] == 0
    assert [args[0] for args, env in engine.calls] == ["/usr/bin/python3"]


@pytest.mark.parametrize("mutation", ["changed-old", "removed-old", "unrelated-new", "partial", "wrong-app",
                                     "wrong-dependency", "held-new", "wrong-architecture"])
def test_retry_refuses_partial_or_foreign_package_state(engine, monkeypatch, mutation):
    assert execute(engine)[0] == 0
    previous = engine.results[-1]
    if mutation == "changed-old":
        engine.installed["labwc"]["version"] = "9.9"
    elif mutation == "removed-old":
        del engine.installed["labwc"]
    elif mutation == "unrelated-new":
        engine.installed["unrelated"] = dict(engine.installed["systemd"])
    elif mutation == "partial":
        del engine.installed["python3-cairo"]
    elif mutation == "wrong-app":
        engine.installed["panelbridge"]["version"] = "9.9"
    elif mutation == "wrong-dependency":
        engine.installed["python3-cairo"]["version"] = "9.9"
    elif mutation == "held-new":
        engine.installed["python3-cairo"]["status"] = "hi "
    else:
        engine.installed["python3-cairo"]["architecture"] = "amd64"
    engine.calls.clear()
    code, result = execute(engine, previous)
    assert code == 4 and result["reason"] == "package_state_partial_or_foreign"
    assert not engine.calls


def test_retry_refuses_payload_and_lifecycle_conflicts_without_any_apt(engine, monkeypatch):
    assert execute(engine)[0] == 0
    previous = engine.results[-1]
    engine.calls.clear()
    def refuse(*args):
        raise engine.module.InstallRefused("installed_payload_mismatch")
    monkeypatch.setattr(engine.module, "_verify_installed", refuse)
    code, result = execute(engine, previous)
    assert code == 4 and result["reason"] == "installed_payload_mismatch"
    assert not engine.calls


@pytest.fixture
def private_store(tmp_path, monkeypatch):
    # Real file operations only inside tmp_path; root ownership is a fake OS
    # boundary. Mode, nofollow, link count, schema, digest and fsync logic is real.
    spec = importlib.util.spec_from_file_location("installer_private_store_test", APP / "packaging/installer-bootstrap.py")
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    # Model trusted system ancestors above the fixture root. Linux pytest uses
    # /tmp (01777); its real permissions are not the simulated /var/lib boundary.
    # Retain every actual mode at and beneath tmp_path so unsafe fixture state
    # still reaches the production checks unchanged.
    ancestors = {(p.stat().st_dev, p.stat().st_ino) for p in tmp_path.parents}
    class FakeOS:
        def __getattr__(self, name):
            return getattr(os, name)
        def fstat(self, fd):
            info = os.fstat(fd)
            mode = info.st_mode & ~0o022 if (info.st_dev, info.st_ino) in ancestors else info.st_mode
            return SimpleNamespace(**{key: getattr(info, key) for key in
                                      ("st_nlink", "st_size", "st_mtime_ns", "st_ctime_ns")}, st_mode=mode,
                                   st_uid=0, st_gid=0)
    monkeypatch.setattr(bootstrap, "os", FakeOS())
    staging = tmp_path / "state/installer-staging"
    staging.mkdir(parents=True, mode=0o700)
    staging.parent.chmod(0o700)
    monkeypatch.setattr(bootstrap, "STAGING", staging)
    stages = []
    for letter in ("a", "b"):
        stage = staging / (letter * 32)
        stage.mkdir(mode=0o700)
        bootstrap._write(stage, "installer.run", b"synthetic reviewed artifact")
        bootstrap._write(stage, "artifact.sha256", digest(b"synthetic reviewed artifact").encode())
        stages.append(stage)
    account = SimpleNamespace(pw_uid=1000, pw_gid=1000, pw_name="fixture-user", pw_dir="/fixture-home")
    decoded = {"package.deb": b"deb", "source.tar.gz": b"source", "package-receipt.json": b"receipt"}
    binding = bootstrap._binding(stages[0], decoded, {"version": "0.1.0~dev1"}, account)
    baseline = {name: {"status": "ii ", "version": "1", "architecture": "arm64"}
                for name in ("labwc", "network-manager", "systemd")}
    journal = {"schema_version": 2, "revision": 1, "previous_sha256": None, "binding": binding,
               "phase": "prepared", "state": "prepared", "reason": None, "baseline": baseline,
               "new_packages": None, "ready_for_install": True, "dependencies_rolled_back": False,
               "live_output_restored": None, "resume_supported": True}
    return SimpleNamespace(module=bootstrap, first=stages[0], next=stages[1], binding=binding, journal=journal)


def test_private_store_keeps_fixture_root_permissions_strict(private_store, tmp_path):
    original = tmp_path.stat().st_mode & 0o7777
    try:
        tmp_path.chmod(0o777)
        with pytest.raises(private_store.module.InstallRefused, match="untrusted_system_directory"):
            private_store.module._root_directory(private_store.first)
    finally:
        tmp_path.chmod(original)


@pytest.mark.parametrize("point", ["before_reference", "pending_reference", "after_reference"])
def test_reference_crash_uses_only_the_fixed_binding(private_store, monkeypatch, point):
    store, module = private_store, private_store.module
    original_write, original_replace = module._write, module.os.replace
    def write(stage, name, data):
        if name == "installer-current.pending" and point == "before_reference":
            raise SimulatedCrash
        original_write(stage, name, data)
    def replace(first, second):
        if Path(first).name == "installer-current.pending":
            if point == "pending_reference":
                raise SimulatedCrash
            original_replace(first, second)
            raise SimulatedCrash
        original_replace(first, second)
    monkeypatch.setattr(module, "_write", write)
    monkeypatch.setattr(module.os, "replace", replace)
    with pytest.raises(SimulatedCrash):
        module._select_attempt(store.first, store.binding)
    monkeypatch.setattr(module, "_write", original_write)
    monkeypatch.setattr(module.os, "replace", original_replace)
    stage, result = module._select_attempt(store.next, store.binding)
    assert stage == (store.next if point == "before_reference" else store.first)
    assert result is None
    reference = json.loads((module.STAGING.parent / "installer-current.json").read_bytes())
    assert reference["attempt"] == stage.name and reference["binding"] == store.binding


@pytest.mark.parametrize("field", ["artifact_sha256", "deb_sha256", "source_sha256", "package_receipt_sha256",
                                   "package_version", "enrolling_uid", "account_fingerprint"])
def test_current_attempt_rejects_changed_inputs_or_account(private_store, field):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    changed = dict(store.binding)
    changed[field] = 1001 if field == "enrolling_uid" else "9.9" if field == "package_version" else "0" * 64
    before = (module.STAGING.parent / "installer-current.json").read_bytes()
    with pytest.raises(module.InstallRefused, match="attempt_binding_conflict"):
        module._select_attempt(store.next, changed)
    assert (module.STAGING.parent / "installer-current.json").read_bytes() == before


def test_missing_reference_never_adopts_a_package_or_orphan_receipt(private_store):
    store, module = private_store, private_store.module
    module._save_result(store.first, store.journal)
    before = (store.first / "result.json").read_bytes()
    with pytest.raises(module.InstallRefused, match="orphan_or_conflicting_attempt"):
        module._select_attempt(store.next, store.binding)
    assert (store.first / "result.json").read_bytes() == before
    assert not (module.STAGING.parent / "installer-current.json").exists()


def test_reference_conflicting_pending_is_preserved(private_store):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    module._write(module.STAGING.parent, "installer-current.pending", b'{"conflict":true}')
    with pytest.raises(module.InstallRefused, match="conflicting_attempt_reference"):
        module._select_attempt(store.next, store.binding)
    assert (module.STAGING.parent / "installer-current.pending").read_bytes() == b'{"conflict":true}'


@pytest.mark.parametrize("point", ["pending_written", "after_replace"])
def test_result_crash_promotes_only_linked_revision_and_retains_original(private_store, monkeypatch, point):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    module._save_result(store.first, store.journal)
    original = (store.first / "result.json").read_bytes()
    store.journal.update(state="installation_incomplete", reason="command_timeout")
    replace = module.os.replace
    def crash(first, second):
        if point == "pending_written":
            raise SimulatedCrash
        replace(first, second)
        raise SimulatedCrash
    monkeypatch.setattr(module.os, "replace", crash)
    with pytest.raises(SimulatedCrash):
        module._save_result(store.first, store.journal)
    monkeypatch.setattr(module.os, "replace", replace)
    stage, journal = module._select_attempt(store.next, store.binding)
    assert stage == store.first and journal["revision"] == 2 and journal["reason"] == "command_timeout"
    assert (store.first / "result-000001.json").read_bytes() == original
    assert not (store.first / "result.pending").exists()


@pytest.mark.parametrize("mutation", ["json", "revision", "binding", "baseline", "history", "orphan", "mode", "hardlink", "symlink"])
def test_corrupt_journal_is_preserved_and_never_replayed(private_store, tmp_path, mutation):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    module._save_result(store.first, store.journal)
    module._save_result(store.first, store.journal)
    target = store.first / "result.json"
    value = json.loads(target.read_bytes())
    if mutation == "json":
        target.write_bytes(b'{"unfinished":')
    elif mutation == "revision":
        value["revision"] = 3
        target.write_bytes(module._canonical(value))
    elif mutation == "binding":
        value["binding"]["enrolling_uid"] = 1001
        target.write_bytes(module._canonical(value))
    elif mutation == "baseline":
        value["baseline"]["labwc"]["version"] = "9.9"
        target.write_bytes(module._canonical(value))
    elif mutation == "history":
        (store.first / "result-000001.json").write_bytes(b"corrupt")
    elif mutation == "orphan":
        module._write(store.first, "result-000099.json", b"orphan")
    elif mutation == "mode":
        target.chmod(0o644)
    elif mutation == "hardlink":
        os.link(target, tmp_path / "extra-link")
    else:
        target.rename(tmp_path / "saved")
        target.symlink_to(tmp_path / "saved")
    before = target.read_bytes()
    with pytest.raises((module.InstallRefused, OSError)):
        module._select_attempt(store.next, store.binding)
    assert target.read_bytes() == before


def test_conflicting_pending_result_does_not_replace_good_receipt(private_store):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    module._save_result(store.first, store.journal)
    before = (store.first / "result.json").read_bytes()
    conflicting = json.loads(before)
    conflicting.update(revision=2, previous_sha256="0" * 64)
    module._write(store.first, "result.pending", module._canonical(conflicting))
    with pytest.raises(module.InstallRefused, match="conflicting_attempt_journal"):
        module._select_attempt(store.next, store.binding)
    assert (store.first / "result.json").read_bytes() == before
    assert (store.first / "result.pending").exists()


@pytest.mark.parametrize("mutation", ["none", "system-uid", "root-gid", "named-uid", "relative", "root-home",
                                     "dot-home", "home-owner", "home-writable", "symlink"])
def test_normal_account_matches_lifecycle_at_passwd_and_home_boundaries(bootstrap, tmp_path, monkeypatch, mutation):
    home = tmp_path / "fixture-home"
    home.mkdir()
    uid, gid = (500 if mutation == "system-uid" else 1000), (0 if mutation == "root-gid" else 1000)
    account = SimpleNamespace(pw_uid=uid, pw_gid=gid, pw_name="fixture-user", pw_dir=str(home))
    if mutation == "relative":
        account.pw_dir = "relative-home"
    elif mutation == "root-home":
        account.pw_dir = "/"
    elif mutation == "dot-home":
        account.pw_dir = str(home / ".." / "fixture-home")
    elif mutation == "symlink":
        (tmp_path / "link").symlink_to(home)
        account.pw_dir = str(tmp_path / "link")
    elif mutation == "home-writable":
        home.chmod(0o777)
    named = SimpleNamespace(**vars(account))
    if mutation == "named-uid":
        named.pw_uid = 1001
    monkeypatch.setattr(bootstrap.pwd, "getpwuid", lambda value: account)
    monkeypatch.setattr(bootstrap.pwd, "getpwnam", lambda value: named)
    class FakeOS:
        def __getattr__(self, name):
            return getattr(os, name)
        def fstat(self, fd):
            info = os.fstat(fd)
            return SimpleNamespace(st_uid=0 if mutation == "home-owner" else uid, st_mode=info.st_mode)
    monkeypatch.setattr(bootstrap, "os", FakeOS())
    if mutation == "none":
        assert bootstrap._normal_account(uid) == account
    else:
        with pytest.raises(bootstrap.InstallRefused, match="invalid_normal_account"):
            bootstrap._normal_account(uid)


def test_enabled_normal_entry_refuses_system_account_before_sudo(bootstrap, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "EXPERIMENTAL_INSTALL_ENABLED", True)
    monkeypatch.setattr(bootstrap, "_bundle", lambda: ({}, {}, {}))
    monkeypatch.setattr(bootstrap, "_lifecycle_present", lambda *args: True)
    monkeypatch.setattr(bootstrap.os, "getuid", lambda: 500)
    monkeypatch.setattr(bootstrap.os, "geteuid", lambda: 500)
    monkeypatch.setattr(bootstrap.subprocess, "call", lambda *args, **kw: pytest.fail("sudo reached"))
    assert bootstrap.main([]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_normal_account"


def test_existing_package_without_owned_attempt_never_runs_commands(engine):
    engine.installed["panelbridge"] = {"status": "ii ", "version": "0.1.0~dev1", "architecture": "arm64"}
    code, result = execute(engine)
    assert code == 4 and result["reason"] == "existing_panelbridge_refused"
    assert not engine.calls and not engine.results


def test_repeated_completed_attempt_only_rechecks_configuration(engine):
    code, result = execute(engine)
    assert code == 0
    engine.calls.clear()
    for _ in range(2):
        code, result = execute(engine, result)
        assert code == 0 and result["state"] == "awaiting_desktop_restart"
    assert [args[0] for args, env in engine.calls] == ["/usr/bin/python3", "/usr/bin/python3"]


def test_changed_passwd_account_on_retry_refuses_before_package_access(engine, monkeypatch):
    assert execute(engine)[0] == 0
    previous = engine.results[-1]
    changed = SimpleNamespace(**vars(engine.account))
    changed.pw_gid = 1001
    monkeypatch.setattr(engine.module, "_normal_account", lambda uid: changed)
    monkeypatch.setattr(engine.module, "_package_state", lambda env: pytest.fail("package access reached"))
    engine.calls.clear()
    code, result = execute(engine, previous)
    assert code == 4 and result["reason"] == "attempt_binding_conflict"
    assert not engine.calls


@pytest.mark.parametrize("mutation", ["none", "uid", "fingerprint", "removing", "corrupt", "early", "missing-completed"])
def test_retry_validates_root_lifecycle_binding_without_rewriting_it(private_store, mutation):
    store, module = private_store, private_store.module
    result = dict(store.journal, phase="configure_started")
    lifecycle = {"schema_version": 1, "normal_uid": 1000, "account_fingerprint": store.binding["account_fingerprint"],
                 "phase": "configuring", "initial_helper": {"active": False, "enabled": False},
                 "operations": {name: "not_started" for name in
                                ("enrollment", "user_configure", "reload", "helper_enable", "helper_start",
                                 "user_restore", "helper_stop", "helper_disable", "enrollment_restore")},
                 "user_setup": None, "user_restore": None}
    if mutation == "uid":
        lifecycle["normal_uid"] = 1001
    elif mutation == "fingerprint":
        lifecycle["account_fingerprint"] = "0" * 64
    elif mutation == "removing":
        lifecycle["phase"] = "pending_desktop_transition"
    elif mutation == "early":
        result["phase"] = "package_transaction_started"
    elif mutation == "missing-completed":
        result["phase"] = "awaiting_desktop_restart"
    data = b"corrupt" if mutation == "corrupt" else module._canonical(lifecycle)
    if mutation != "missing-completed":
        module._write(module.STAGING.parent, "maintenance.json", data)
    if mutation == "none":
        module._maintenance_binding(result)
    else:
        with pytest.raises(module.InstallRefused):
            module._maintenance_binding(result)
    if mutation != "missing-completed":
        assert (module.STAGING.parent / "maintenance.json").read_bytes() == data


def test_first_pending_result_recovers_after_crash_before_initial_replace(private_store, monkeypatch):
    store, module = private_store, private_store.module
    module._select_attempt(store.first, store.binding)
    replace = module.os.replace
    monkeypatch.setattr(module.os, "replace", lambda *args: (_ for _ in ()).throw(SimulatedCrash()))
    with pytest.raises(SimulatedCrash):
        module._save_result(store.first, store.journal)
    monkeypatch.setattr(module.os, "replace", replace)
    stage, result = module._select_attempt(store.next, store.binding)
    assert stage == store.first and result["phase"] == "prepared" and result["revision"] == 1
    assert not (stage / "result.pending").exists()


def test_fake_engine_persistent_journal_can_resume_after_success_before_final_fsync(engine, private_store, monkeypatch):
    # Share only the journal implementation and private file boundaries with the
    # fake command engine, so persistence is exercised rather than mocked away.
    store, module = private_store, engine.module
    module._private_read = store.module._private_read
    module._root_directory = store.module._root_directory
    module.os = store.module.os
    monkeypatch.setattr(module, "STAGING", store.module.STAGING)
    original_save = store.module._save_result
    saved = []
    def save(stage, result):
        if result["phase"] == "awaiting_desktop_restart":
            raise SimulatedCrash
        original_save(stage, result)
        saved.append(result["phase"])
    monkeypatch.setattr(module, "_save_result", save)
    decoded = {"package.deb": b"package", "source.tar.gz": b"source", "package-receipt.json": b"receipt"}
    receipt = {"version": "0.1.0~dev1"}
    binding = module._binding(store.first, decoded, receipt, engine.account)
    store.module._select_attempt(store.first, binding)
    with pytest.raises(SimulatedCrash):
        module._execute(store.first, decoded, receipt, {}, engine.account)
    assert saved[-1] == "configure_started"
    engine.calls.clear()
    selected, result = store.module._select_attempt(store.next, binding)
    monkeypatch.setattr(module, "_save_result", original_save)
    code, final = module._execute(selected, decoded, receipt, {}, engine.account, resume=result)
    assert code == 0 and final["phase"] == "awaiting_desktop_restart"
    assert [args[0] for args, env in engine.calls] == ["/usr/bin/python3"]
    assert (selected / "result-000001.json").exists()


def test_dpkg_audit_failure_precedes_inventory_query(bootstrap, monkeypatch):
    calls = []
    def check(args, env, **options):
        calls.append(args)
        return b"incomplete package transaction"
    monkeypatch.setattr(bootstrap, "_checked", check)
    with pytest.raises(bootstrap.InstallRefused, match="dpkg_requires_recovery"):
        bootstrap._package_state({})
    assert calls == [["/usr/bin/dpkg", "--audit"]]


def test_installed_hook_bytes_are_part_of_payload_revalidation(private_store, tmp_path, monkeypatch):
    module = private_store.module
    directory = tmp_path / "dpkg-info"
    directory.mkdir()
    hook = directory / "panelbridge.prerm"
    hook.write_bytes(b"reviewed hook")
    hook.chmod(0o755)
    seen = []
    def parent(path):
        seen.append(str(path))
        assert str(path) == "/var/lib/dpkg/info"
        return os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.setattr(module, "_root_directory", parent)
    receipt = {"payload_files": [{"path": "DEBIAN/prerm", "mode": 0o755, "size": 13,
                                  "sha256": digest(b"reviewed hook")}]}
    module._verify_installed(receipt)
    assert seen == ["/var/lib/dpkg/info"]
    hook.write_bytes(b"changed bytes")
    with pytest.raises(module.InstallRefused, match="installed_payload_mismatch"):
        module._verify_installed(receipt)
