"""Durable, private evidence for one live-controller attempt."""

from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
from typing import cast

import pytest

from hccast_wired.live.evidence import RunEvidenceWriter
from hccast_wired.live.model import DesiredMode, LiveConfig, RuntimePhase
from hccast_wired.live.supervisor import (
    AttemptClassification,
    AttemptEvent,
    AttemptResult,
    CleanupError,
    CleanupResult,
)


UTC_NOW = datetime(2026, 7, 21, 18, 30, 45, tzinfo=timezone.utc)


def _config(*, kiosk_url: str = "https://example.test:8443/dashboard?token=secret#section") -> LiveConfig:
    return LiveConfig(mode=DesiredMode.KIOSK, kiosk_url=kiosk_url)


def _result() -> AttemptResult:
    return AttemptResult(
        classification=AttemptClassification.FAILURE,
        error="gadget-exited: 1",
        product="HCT-AT01",
        version="2505161526",
        streaming_duration=1.25,
        cleanup=CleanupResult(
            attempted_actions=("gadget-cleanup", "stock-restore"),
            errors=(CleanupError(action="stock-restore", message="service inactive"),),
            verified_postconditions=("hccast absent",),
            success=False,
        ),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_start_creates_private_utc_token_attempt_and_redacted_effective_config(tmp_path: Path) -> None:
    writer = RunEvidenceWriter.start(
        tmp_path / "evidence",
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="a7K_91",
    )

    assert writer.path.name == "20260721T183045Z-a7K_91"
    assert _mode(writer.root) == 0o700
    assert _mode(writer.path) == 0o700
    effective_config = writer.path / "effective-config.json"
    assert _mode(effective_config) == 0o600
    assert json.loads(effective_config.read_text(encoding="utf-8"))["kiosk_url"] == (
        "https://example.test:8443/dashboard"
    )


def test_start_rejects_credential_bearing_url_even_for_unvalidated_config(tmp_path: Path) -> None:
    unsafe = cast(
        LiveConfig,
        SimpleNamespace(
            to_mapping=lambda: {"kiosk_url": "https://user:password@example.test/path"},
        ),
    )

    with pytest.raises(ValueError, match="credentials"):
        RunEvidenceWriter.start(
            tmp_path / "evidence",
            unsafe,
            utc_now=lambda: UTC_NOW,
            attempt_token="unsafe",
        )


def test_recording_appends_thread_safe_artifacts_and_finalize_marks_completion(tmp_path: Path) -> None:
    writer = RunEvidenceWriter.start(
        tmp_path / "evidence",
        _config(kiosk_url="https://example.test/view"),
        utc_now=lambda: UTC_NOW,
        attempt_token="recording",
    )

    writer.record_transition(AttemptEvent(RuntimePhase.WAITING_FOR_SCREEN))
    writer.record_transition(AttemptEvent(RuntimePhase.STREAMING, product="HCT-AT01", version="2505161526"))
    writer.append_log("source", "source stdout\n")
    writer.append_log("encoder", b"encoder stderr\n")
    writer.append_log("gadget", "gadget stdout\n")
    writer.finalize(_result(), terminal_reason="gadget-exited")

    transitions = [
        json.loads(line)
        for line in (writer.path / "transitions.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [transition["phase"] for transition in transitions] == [
        RuntimePhase.WAITING_FOR_SCREEN.value,
        RuntimePhase.STREAMING.value,
    ]
    assert (writer.path / "source.log").read_bytes() == b"source stdout\n"
    assert (writer.path / "encoder.log").read_bytes() == b"encoder stderr\n"
    assert (writer.path / "gadget.log").read_bytes() == b"gadget stdout\n"
    result = json.loads((writer.path / "RESULT.json").read_text(encoding="utf-8"))
    assert result["terminal_reason"] == "gadget-exited"
    assert result["classification"] == "failure"
    assert result["cleanup"]["errors"] == [
        {"action": "stock-restore", "message": "service inactive"}
    ]
    assert _mode(writer.path / "RESULT.json") == 0o600


def _complete_attempt(root: Path, name: str, *, bytes_written: int = 0) -> Path:
    attempt = root / name
    attempt.mkdir(mode=0o700)
    result = attempt / "RESULT.json"
    result.write_text("{}", encoding="utf-8")
    result.chmod(0o600)
    if bytes_written:
        payload = attempt / "payload.bin"
        with payload.open("wb") as handle:
            handle.truncate(bytes_written)
        payload.chmod(0o600)
    return attempt


def test_finalize_prunes_oldest_complete_attempts_but_never_active_one(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    oldest = _complete_attempt(root, "20260721T180000Z-oldest")
    for index in range(1, 20):
        _complete_attempt(root, f"20260721T1800{index:02d}Z-prior")
    writer = RunEvidenceWriter.start(
        root,
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="active",
    )

    writer.finalize(_result(), terminal_reason="gadget-exited")

    assert not oldest.exists()
    assert writer.path.is_dir()
    assert len([path for path in root.iterdir() if path.is_dir()]) == 20


def test_finalize_prunes_by_logical_bytes_without_pruning_incomplete_attempts(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    old_complete = _complete_attempt(root, "20260721T180000Z-large", bytes_written=201 * 1024 * 1024)
    incomplete = root / "20260721T180100Z-incomplete"
    incomplete.mkdir(mode=0o700)
    (incomplete / "diagnostic.log").write_text("retain me", encoding="utf-8")
    writer = RunEvidenceWriter.start(
        root,
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="active",
    )

    writer.finalize(_result(), terminal_reason="gadget-exited")

    assert not old_complete.exists()
    assert incomplete.is_dir()
    assert writer.path.is_dir()


def test_refuses_symlink_root_and_never_follows_candidate_symlinks(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        RunEvidenceWriter.start(
            linked_root,
            _config(),
            utc_now=lambda: UTC_NOW,
            attempt_token="blocked",
        )

    root = tmp_path / "evidence"
    root.mkdir(mode=0o700)
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    (sentinel / "do-not-touch").write_text("safe", encoding="utf-8")
    malicious = root / "20260721T180000Z-symlink"
    malicious.symlink_to(sentinel, target_is_directory=True)
    writer = RunEvidenceWriter.start(
        root,
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="active",
    )

    writer.finalize(_result(), terminal_reason="gadget-exited")

    assert malicious.is_symlink()
    assert (sentinel / "do-not-touch").read_text(encoding="utf-8") == "safe"


def test_start_rejects_ancestor_symlink_before_creating_through_it(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        RunEvidenceWriter.start(
            alias / "evidence",
            _config(),
            utc_now=lambda: UTC_NOW,
            attempt_token="ancestor",
        )

    assert list(target.iterdir()) == []


def test_start_rejects_dotdot_that_would_hide_symlink_redirection(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    target = outside / "target"
    target.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="normalized"):
        RunEvidenceWriter.start(
            alias / ".." / "redirected",
            _config(),
            utc_now=lambda: UTC_NOW,
            attempt_token="dotdot",
        )

    assert not (outside / "redirected").exists()
    assert not (tmp_path / "redirected").exists()


def test_start_rejects_relative_root_before_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        RunEvidenceWriter.start(
            Path("relative/evidence"),
            _config(),
            utc_now=lambda: UTC_NOW,
            attempt_token="relative",
        )

    assert not (tmp_path / "relative").exists()


def test_concurrent_log_appends_preserve_every_complete_record(tmp_path: Path) -> None:
    writer = RunEvidenceWriter.start(
        tmp_path / "evidence",
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="concurrent",
    )
    worker_count = 24
    records_per_worker = 25
    start_together = threading.Barrier(worker_count)
    expected = [
        f"{worker:02d}:{record:02d}:{'x' * 1024}\n".encode()
        for worker in range(worker_count)
        for record in range(records_per_worker)
    ]

    def append_records(worker: int) -> None:
        start_together.wait(timeout=5)
        for record in range(records_per_worker):
            writer.append_log("source", f"{worker:02d}:{record:02d}:{'x' * 1024}\n")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(append_records, worker) for worker in range(worker_count)]
        for future in futures:
            future.result(timeout=10)

    actual = (writer.path / "source.log").read_bytes().splitlines(keepends=True)
    assert len(actual) == worker_count * records_per_worker
    assert Counter(actual) == Counter(expected)


def test_log_append_opens_with_no_follow_protection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    writer = RunEvidenceWriter.start(
        tmp_path / "evidence",
        _config(),
        utc_now=lambda: UTC_NOW,
        attempt_token="nofollow",
    )
    original_open = os.open
    observed_flags: list[int] = []

    def observe_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(path).name == "source.log":
            observed_flags.append(flags)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", observe_open)
    writer.append_log("source", "safe\n")

    assert observed_flags
    assert observed_flags[-1] & os.O_NOFOLLOW
