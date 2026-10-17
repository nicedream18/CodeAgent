import importlib.util
import io
import json
import tarfile
from pathlib import Path

from mokioclaw.evaluation.models import EvalTrialConfig, EvalTrialResult, Grade

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/report_job_readiness.py"
spec = importlib.util.spec_from_file_location("goal2_report", SCRIPT)
assert spec and spec.loader
reporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reporter)


def test_partial_pass_unknown_claim_and_cache_are_not_success_or_zero(tmp_path):
    config = EvalTrialConfig(task_id="M02-v2", architecture="full", repeat=1,
                             model="offline", model_config_sha256="a", harness_revision="b",
                             prompt_sha256="c")
    result = EvalTrialResult(experiment_id="offline", trial_id=config.trial_id, config=config,
                             task_sha256="d", execution_status="failed", real_model=True,
                             stop_reason="token_budget_exhausted", run_id="run", stream_ids=["s"],
                             model_claim=True, grade=Grade(valid=True, solved=True,
                                                         target_passed=1, tests_run=1),
                             model_metrics={"requests": 1, "input_tokens": 10, "output_tokens": 2,
                                            "total_tokens": 12, "cache_read_tokens": 4,
                                            "cache_creation_tokens": None, "cache_usage_incomplete": True})
    (tmp_path / "result.json").write_text(result.model_dump_json())
    events = [{"type": "custom:llm_request", "run_id": "run", "payload": {"payload": {
        "type": "llm_request", "role": "verifier", "stream_id": "s"}}},
        {"type": "custom:model_usage", "run_id": "run", "payload": {"payload": {
            "type": "model_usage", "role": "verifier", "stream_id": "s", "input_tokens": 10,
            "output_tokens": 2, "total_tokens": 12, "cache_read_tokens": 4}}}]
    with tarfile.open(tmp_path / "trace.tar.gz", "w:gz") as archive:
        for name, text in {
            ".mokioclaw/traces/t/events.jsonl": "\n".join(json.dumps(e) for e in events),
            ".mokioclaw/checkpoints/checkpoint.json": json.dumps({"summary": {
                "passed": True, "final_answer": "No explicit claim marker"}}),
        }.items():
            info = tarfile.TarInfo(name)
            data = text.encode()
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    row = reporter.observations(tmp_path, "B1")
    assert row["additional_audit_issues"] == []
    assert row["task_solved"] is False and row["partial_artifact_passed"] is True
    assert row["explicit_final_model_claim"] is None
    assert row["legacy_worker_claim"] is True and row["verifier_passed"] is True
    summary = reporter.summarize([row], 9)
    assert summary["complete_deliveries"] == 0 and summary["scheduled_denominator"] == 9
    assert summary["usage"]["total_tokens"]["known_sum"] == 12
    assert summary["usage"]["cache_creation_tokens"]["known_sum"] is None
    assert summary["usage"]["cache_read_tokens"]["known_sum"] == 4
    assert summary["requests_without_usage"] == 0
    assert summary["complete_delivery_seconds"]["n"] == 0
    assert summary["non_delivery_seconds"]["n"] == 1


def test_empty_distribution_remains_unknown():
    assert reporter.distribution([]) == {"n": 0, "min": None, "median": None, "max": None}


def test_claim_requires_an_explicit_final_line():
    assert reporter.claim_marker("Done\nAGENT_CLAIM: solved\n") is True
    assert reporter.claim_marker("AGENT_CLAIM: unsolved") is False
    assert reporter.claim_marker("Example AGENT_CLAIM: solved\nStill working") is None
