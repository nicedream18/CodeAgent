"""Offline tests only: no API requests, network, containers or real experiment writes."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

DRIVER = Path(__file__).resolve().parents[1] / "scripts/job_readiness_trials.py"
spec = importlib.util.spec_from_file_location("goal2_driver", DRIVER)
assert spec and spec.loader
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)


def test_started_receipt_and_claim_never_retry(tmp_path):
    assert driver.action_for(tmp_path) == "start"
    (tmp_path / "dispatch.json").write_text("{}")
    with pytest.raises(RuntimeError, match="will not rerun"):
        driver.action_for(tmp_path)
    (tmp_path / "result.json").write_text('{"execution_status":"running"}')
    with pytest.raises(RuntimeError, match="will not rerun"):
        driver.action_for(tmp_path)
    (tmp_path / "result.json").write_text('{"execution_status":"failed"}')
    assert driver.action_for(tmp_path) == "skip_terminal"


def test_exact_order_version_budget_and_resume(tmp_path, monkeypatch):
    plan = driver.fixed_input()
    monkeypatch.setenv("MODEL", "offline-test-only")
    monkeypatch.setenv("BASE_URL", "http://invalid.example")
    monkeypatch.setenv("API_KEY", "never-used")
    probe = {"input_sha256": "fixed", "versions": {
        name: {"offline_controls": {"requests": {"real_model": False}}} for name in ("B0", "B1")}}
    driver.write_json(tmp_path / "batches/test/offline-preflight.json", probe)
    starts = []

    def fake_dispatch(spec, directory, checkout):
        # Manifest and receipt are present before the simulated first request.
        manifest = driver.read(directory.parents[1] / "manifest.json")
        assert manifest["authorization"]["max_trials_total"] == 18
        directory.mkdir(parents=True)
        (directory / "dispatch.json").write_text(json.dumps(spec))
        config = spec["config"]
        version = plan["trials"][len(starts)]["version"]
        assert checkout == tmp_path / "checkouts" / version
        assert config["harness_revision"] == plan["versions"][version]["revision"]
        assert (config["token_limit"], config["request_limit"], config["tool_limit"],
                config["generation_seconds"], config["bash_seconds"], config["grading_seconds"],
                config["context_tokens"], config["output_tokens"], config["temperature"]) == (
                    200000, 40, 80, 900, 60, 120, 32768, 4096, 0)
        starts.append((version, config["task_id"], config["repeat"]))
        driver.write_json(directory / "result.json", {
            "execution_status": "failed", "task_solved": False, "grade": {"solved": False},
            "model_metrics": {"requests": 0}, "stop_reason": "offline_control",
            "infrastructure_error": None})

    with patch.object(driver, "preflight", return_value=probe), \
         patch.object(driver, "verify_version", return_value={}), \
         patch.object(driver, "verify_tasks"), patch.object(driver, "load_dotenv"), \
         patch.object(driver.subprocess, "run"), \
         patch.object(driver, "dispatch", side_effect=fake_dispatch):
        driver.run_matrix(tmp_path, "test", plan)
        driver.run_matrix(tmp_path, "test", plan)
    assert starts == [(t["version"], t["task_id"], t["repeat"]) for t in plan["trials"]]
    assert len(starts) == 18
    assert len(list((tmp_path / "experiments").glob("*/trials/*/result.json"))) == 18


def test_changed_source_and_wrong_worker_import_refused(tmp_path):
    with patch.object(driver, "_source_contract", return_value={}):
        with pytest.raises(ValueError, match="mismatch"):
            driver.verify_version(tmp_path, driver.fixed_input()["versions"]["B0"])
    result = type("Result", (), {"stdout": json.dumps({"modules": {"worker": "/wrong/src.py"}})})()
    with patch.object(driver.subprocess, "run", return_value=result):
        with pytest.raises(ValueError, match="outside"):
            driver.import_probe(tmp_path)


def test_preview_does_not_start_anything():
    with patch.object(driver.sys, "argv", [str(DRIVER)]), \
         patch.object(driver, "run_matrix") as run, \
         patch.object(driver, "preflight") as preflight:
        driver.main()
    run.assert_not_called()
    preflight.assert_not_called()
