"""No-network regressions for experiment validity, ownership and freezing."""

import json
import os
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from mokioclaw.evaluation import process_control, runner
from mokioclaw.evaluation.grader import MAX_TEST_REPORT_BYTES, _counts
from mokioclaw.evaluation.models import EvalExperimentValidity


def test_invalid_experiment_remains_readable_but_cannot_be_a_comparison(tmp_path):
    root = tmp_path / "experiments/invalid"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"trials": [1, 2]}))
    validity = EvalExperimentValidity(status="invalid", issues=["Child profile lost"])
    (root / "validity.json").write_text(validity.model_dump_json())
    report = runner.report_experiment(tmp_path, "invalid")
    assert not report["comparison_valid"]
    assert report["not_started"] == 2
    assert report["validity"]["issues"] == ["Child profile lost"]
    assert "不能据此比较架构优劣" in (root / "report.md").read_text()
    (root / "validity.json").unlink()
    assert runner.report_experiment(tmp_path, "invalid")["validity"]["status"] == "unreviewed"


def test_frozen_source_or_task_change_prevents_next_trial(tmp_path, monkeypatch):
    task = tmp_path / "M07/task.json"
    task.parent.mkdir()
    task.write_text("old")
    manifest = {"harness_revision": "old", "tasks": {"M07": runner.sha(b"old")}}
    monkeypatch.setattr(runner, "_source_contract", lambda _: {"harness_revision": "old"})
    runner._verify_frozen(manifest, tmp_path, task)
    task.write_text("new")
    with pytest.raises(ValueError, match="Frozen task changed"):
        runner._verify_frozen(manifest, tmp_path, task)
    task.write_text("old")
    monkeypatch.setattr(runner, "_source_contract", lambda _: {"harness_revision": "new"})
    with pytest.raises(ValueError, match="Frozen source changed"):
        runner._verify_frozen(manifest, tmp_path, task)


@pytest.mark.skipif(os.name == "nt", reason="Process groups use POSIX ownership")
def test_recovery_only_signals_worker_with_matching_nonce_and_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(process_control, "cleanup_owner", lambda _: [])
    token = "a" * 32
    process = subprocess.Popen(
        [
            sys.executable, "-c", "import time; time.sleep(60)",
            "mokioclaw.evaluation.worker", str((tmp_path / "spec.json").resolve()), token,
        ],
        start_new_session=True,
    )
    identity = tmp_path / "worker-process.json"
    try:
        # A reused PID or another run with the same module is insufficient.
        identity.write_text(json.dumps({"pid": process.pid, "token": "b" * 32}))
        assert not process_control.stop_worker(tmp_path, "owned")
        assert process.poll() is None
        identity.write_text(json.dumps({"pid": process.pid, "token": token}))
        assert process_control.stop_worker(tmp_path, "owned")
        process.wait(timeout=5)
        assert process.returncode != 0
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)


def test_worker_cancels_when_runner_dies_and_cleans_only_its_owner(tmp_path, monkeypatch):
    cancelled, cleaned = threading.Event(), threading.Event()
    owners = []
    monkeypatch.setattr(process_control.os, "getppid", lambda: 123)

    def cleanup(owner):
        owners.append(owner)
        cleaned.set()
        return []

    monkeypatch.setattr(process_control, "cleanup_owner", cleanup)
    watcher = process_control.watch_parent(
        SimpleNamespace(cancel=cancelled.set),
        {"parent_pid": 123, "worker_token": "a" * 32, "owner": "this-trial"},
        tmp_path / "spec.json",
    )
    try:
        monkeypatch.setattr(process_control.os, "getppid", lambda: 456)
        assert cancelled.wait(2) and cleaned.wait(2)
        assert owners == ["this-trial"]
    finally:
        watcher.set()


def test_large_or_linked_junit_report_is_rejected(tmp_path):
    xml = tmp_path / "large.xml"
    with xml.open("wb") as handle:
        handle.truncate(MAX_TEST_REPORT_BYTES + 1)
    assert _counts(xml) == (0, 0, 0, 0)
    link = tmp_path / "linked.xml"
    link.symlink_to(xml)
    assert _counts(link) == (0, 0, 0, 0)
