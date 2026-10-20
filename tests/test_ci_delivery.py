"""CI failure boundaries are exercised offline, without models or a remote runner."""

import importlib.util
import io
import json
import shlex
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def load(name, alias=None):
    spec = importlib.util.spec_from_file_location(alias or name, SCRIPTS / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias or name] = module
    spec.loader.exec_module(module)
    return module


support = load("ci_support")
checks = load("check_docker")
runner = load("ci_report")
review = load("ci_push_manifest")


def test_child_environment_has_no_models_or_personal_config(tmp_path, monkeypatch):
    for key in ("API_KEY", "OPENAI_API_KEY", "MODEL", "BASE_URL", "GITHUB_TOKEN", "PYTHONPATH"):
        monkeypatch.setenv(key, "private-test-value")
    env = support.child_environment(tmp_path)
    assert not set(env) & {"API_KEY", "OPENAI_API_KEY", "MODEL", "BASE_URL", "GITHUB_TOKEN", "PYTHONPATH"}
    assert env["HOME"] == str(tmp_path) and env["PYTHON_DOTENV_DISABLED"] == "1"
    assert support.credential_values({"API_KEY": "private-test-value",
                                       "MOKIO_CONTEXT_TOKEN_LIMIT": "123456789"}) == ["private-test-value"]


def test_output_is_bounded_redacted_and_still_drained(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "private-test-value")
    script = "import sys; print('Authorization: Bearer private-test-value'); " \
             "print('x'*3000000); print('tail-marker')"
    result = support.run_command([sys.executable, "-c", script], cwd=tmp_path,
                                 environment=support.child_environment(tmp_path),
                                 log=tmp_path / "output.log", limit=128)
    assert result["status"] == "passed" and result["exit_code"] == 0
    assert result["output_bytes"] >= 3000000 and result["log_truncated"]
    assert (tmp_path / "output.log").stat().st_size <= 128
    assert "private-test-value" not in json.dumps(result)
    assert "private-test-value" not in (tmp_path / "output.log").read_text()
    assert "tail-marker" in result["tail"]


def test_timeout_kills_only_its_process_group(tmp_path):
    code = "import subprocess,time; subprocess.Popen(['sh','-c','sleep 0.5; touch late']); time.sleep(5)"
    result = support.run_command([sys.executable, "-c", code], cwd=tmp_path,
                                 environment=support.child_environment(tmp_path),
                                 log=tmp_path / "timeout.log", timeout=0.1)
    assert result["status"] == "failed" and result["timed_out"]
    time.sleep(0.7)
    assert not (tmp_path / "late").exists()


def test_unavailable_engine_does_not_start_pytest(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "doctor", lambda *_: {"ok": False, "backend": "docker", "error": "off"})
    monkeypatch.setattr(checks, "run_command", lambda *_a, **_k: pytest.fail("must not execute"))
    result = checks.check(tmp_path)
    assert result["ok"] is False and (tmp_path / "result.json").exists()


def test_docker_all_skipped_stale_or_missing_receipts_fail():
    assert not checks.credible_tests({}, "one")
    assert not checks.credible_tests({"session_token": "one", "exit_code": 0,
                                      "passed_nodeids": [], "counts": {"skipped": 29}}, "one")
    good = {"session_token": "one", "exit_code": 0,
            "passed_nodeids": sorted(checks.REQUIRED), "counts": {"passed": 29}}
    assert checks.credible_tests(good, "one")
    assert not checks.credible_tests(good, "two")


def test_pytest_receipt_keeps_counts_and_required_nodeids(tmp_path, monkeypatch):
    plugin = load("ci_pytest_plugin", "ci_pytest_plugin_test")
    monkeypatch.setattr(plugin, "NESTED", False)
    destination = tmp_path / "receipt.json"
    monkeypatch.setenv("CI_PYTEST_REPORT", str(destination))
    monkeypatch.setenv("CI_REPORT_TOKEN", "nonce")
    stats = {"passed": [SimpleNamespace(nodeid=n) for n in sorted(checks.REQUIRED)],
             "skipped": [SimpleNamespace()]}
    manager = SimpleNamespace(getplugin=lambda _: SimpleNamespace(stats=stats))
    session = SimpleNamespace(config=SimpleNamespace(pluginmanager=manager), testscollected=3)
    plugin.pytest_sessionfinish(session, 0)
    value = json.loads(destination.read_text())
    assert value["counts"]["passed"] == 2 and value["counts"]["skipped"] == 1
    assert checks.credible_tests(value, "nonce")


def test_failed_step_still_produces_report(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.Checks, "step", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError()))
    assert not runner.run("wheel", tmp_path / "report")
    value = json.loads((tmp_path / "report/report.json").read_text())
    assert value["status"] == "failed" and (tmp_path / "report/summary.md").is_file()
    assert all(c["status"] == "not_started" for c in value["checks"])


def test_existing_evidence_is_not_overwritten(tmp_path):
    (tmp_path / "report.json").write_text("original")
    with pytest.raises(ValueError, match="never overwritten"):
        runner.Checks("check", tmp_path)
    assert (tmp_path / "report.json").read_text() == "original"


def test_workflow_keeps_three_jobs_manual_trigger_and_failure_artifacts():
    workflow = yaml.safe_load((SCRIPTS.parent / ".github/workflows/check.yml").read_text())
    assert set(workflow["on"]) == {"push", "pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"check", "docker", "wheel"}
    for name, job in workflow["jobs"].items():
        assert job["runs-on"] == "ubuntu-24.04" and job["timeout-minutes"] > 0
        upload = [step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")]
        assert len(upload) == 1 and upload[0]["if"] == "always()"
        assert upload[0]["with"]["retention-days"] == 14
        entry = [s["run"] for s in job["steps"] if f"--job {name}" in s.get("run", "")]
        assert len(entry) == 1
        # setup-uv only sets UV_PYTHON; the runner's bare python can still be 3.12.
        version = next(s["with"]["python-version"] for s in job["steps"]
                       if "astral-sh/setup-uv@" in s.get("uses", ""))
        assert shlex.split(entry[0])[:7] == [
            "uv", "run", "--no-project", "--python", version, "python", "scripts/ci_report.py"]
        assert not any(s.get("continue-on-error") for s in job["steps"])
    assert "secrets." not in json.dumps(workflow)


def test_push_scan_checks_archive_members_without_echoing_credentials():
    value = b"private-fixture-value"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        item = tarfile.TarInfo("data/record.txt")
        item.size = len(value)
        archive.addfile(item, io.BytesIO(value))
    findings = review.inspect_bytes(stream.getvalue(), "trace.tar.gz", [value])
    assert findings == [{"path": "trace.tar.gz:data/record.txt", "reason": "configured_credential_value"}]
    assert value.decode() not in json.dumps(findings)


def test_push_scan_does_not_claim_unexamined_archives_passed(monkeypatch):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        item = tarfile.TarInfo("large.txt")
        item.size = 100
        archive.addfile(item, io.BytesIO(b"x" * 100))
    monkeypatch.setattr(review, "MAX_BLOB", 8)
    assert review.inspect_bytes(stream.getvalue(), "trace.tar.gz", []) == [
        {"path": "trace.tar.gz", "reason": "archive_quota_not_scanned"}]


def test_unavailable_grading_image_does_not_start_pytest(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKIO_DOCKER_IMAGE", "current")
    monkeypatch.setenv("MOKIO_EVAL_DOCKER_IMAGE", "missing-frozen")
    monkeypatch.setattr(checks, "doctor", lambda _, image: {
        "ok": image == "current", "image_digest": "sha256:current"})
    monkeypatch.setattr(checks, "run_command", lambda *_a, **_k: pytest.fail("must not execute"))
    result = checks.check(tmp_path)
    assert not result["ok"] and not result["grading_image"]["ok"]


def test_docker_checks_keep_execution_and_frozen_grading_images_separate(tmp_path, monkeypatch):
    monkeypatch.setenv("MOKIO_DOCKER_IMAGE", "current")
    monkeypatch.setenv("MOKIO_EVAL_DOCKER_IMAGE", "frozen")
    monkeypatch.setattr(checks, "doctor", lambda _, image: {
        "ok": True, "image_digest": "sha256:" + image})
    monkeypatch.setattr(checks, "docker_cli", lambda *_: SimpleNamespace(stdout=""))

    def run(_command, *, environment, **_kwargs):
        assert environment["MOKIO_DOCKER_IMAGE"] == "sha256:current"
        assert environment["MOKIO_EVAL_DOCKER_IMAGE"] == "sha256:frozen"
        Path(environment["CI_PYTEST_REPORT"]).write_text(json.dumps({
            "session_token": environment["CI_REPORT_TOKEN"], "exit_code": 0,
            "passed_nodeids": sorted(checks.REQUIRED), "counts": {"passed": 2}}))
        return {"status": "passed"}

    monkeypatch.setattr(checks, "run_command", run)
    result = checks.check(tmp_path)
    assert result["ok"] and result["image_digest"] == "sha256:current"
    assert result["grading_image"]["image_digest"] == "sha256:frozen"
