import json
from pathlib import Path

import pytest

from mokioclaw.evaluation.job_tasks import TASK_IDS, prepare_job_task
from mokioclaw.evaluation.overhead import analyze_overhead


@pytest.mark.parametrize("name", TASK_IDS)
def test_v2_preparation_is_separate_frozen_and_exposes_only_basics(tmp_path, monkeypatch, name):
    monkeypatch.setattr("mokioclaw.evaluation.prepare.resolve_image", lambda _: "sha256:fixture")
    monkeypatch.setattr("mokioclaw.evaluation.job_tasks.resolve_image", lambda _: "sha256:fixture")
    old = tmp_path / "internship-v1/sentinel"
    old.parent.mkdir()
    old.write_bytes(b"preserve old cache")
    cache = tmp_path / "job-readiness-v1"
    task = prepare_job_task(name, cache, Path.cwd())
    directory = cache / "tasks" / name
    base = directory / "base"
    assert task.id == name and task.version == "job-readiness-v1"
    assert (base / "examples/mcp/echo_server.py").is_file()
    assert [p.name for p in (base / "tests").iterdir()] == ["test_visible_basics.py"]
    assert not (base / "reference.patch").exists()
    assert not (base / "trusted-tests").exists()
    assert task.target_tests and task.regression_tests
    assert old.read_bytes() == b"preserve old cache"
    assert prepare_job_task(name, cache, Path.cwd()) == task
    (base / "examples/mcp/echo_server.py").unlink()
    with pytest.raises(ValueError, match="Frozen v2 task changed"):
        prepare_job_task(name, cache, Path.cwd())


def test_role_diagnostics_deduplicate_usage_and_preserve_unknown_cache():
    events = [
        {"type": "llm_request", "stream_id": "s", "node": "codeAgent"},
        {"type": "model_usage", "stream_id": "s", "agent": "codeAgent", "input_tokens": 10,
         "output_tokens": 2, "total_tokens": 12, "duration_ms": 5, "estimated": True,
         "cache_read_tokens": 0, "cache_creation_tokens": None},
    ]
    result = analyze_overhead(events + events + [
        {"type": "llm_stream_end", "stream_id": "s"},
        {"type": "llm_stream_end", "stream_id": "s"},
        {"type": "run_budget_exhausted", "stop_reason": "token_budget_exhausted"},
        {"type": "run_end", "payload": {"status": "failed", "latest_node": "codeAgent"}},
    ])
    stats = result["roles"]["codeAgent"]
    assert result["requests"] == stats["requests"] == stats["usage_records"] == 1
    assert stats["input_tokens"] == 10 and stats["total_tokens"] == 12
    assert stats["estimated"] and stats["cache_read_tokens"] == 0
    assert stats["cache_creation_tokens"] is None and stats["cache_usage_incomplete"]
    assert result["duplicate_stream_end_observations"] == 1
    assert result["execution_status"] == "failed" and result["latest_node"] == "codeAgent"
    assert result["stop_reason"] == "token_budget_exhausted"


def test_grader_does_not_stage_executable_tests_in_report_directory(tmp_path, monkeypatch):
    from mokioclaw.core.execution_backend import ExecutionResult
    from mokioclaw.evaluation.grader import grade_candidate

    monkeypatch.setattr("mokioclaw.evaluation.prepare.resolve_image", lambda _: "sha256:fixture")
    task = prepare_job_task("M02-v2", tmp_path / "cache", Path.cwd())
    report = tmp_path / "report"

    def inspect(self, request):
        assert not request.workspace.is_relative_to(report)
        assert (request.workspace / "tests/test_generation_reliability.py").is_file()
        return ExecutionResult(False, 2, False, "fixture: no tests executed", "", 0, "docker")

    monkeypatch.setattr("mokioclaw.evaluation.grader.DockerBackend.run", inspect)
    result = grade_candidate(task, tmp_path / "cache/tasks/M02-v2",
                             tmp_path / "cache/tasks/M02-v2/base", report)
    assert not result.valid and not result.solved


def test_read_repeat_diagnostics_require_same_arguments_output_and_context():
    events = []
    for i, (content, version) in enumerate([("same", 0), ("same", 0), ("changed", 0), ("same", 1)]):
        events += [
            {"type": "tool_call", "node": "codeAgent", "name": "FileReadTool",
             "args": {"file_path": "a.py"}},
            {"type": "tool_result", "node": "codeAgent", "name": "FileReadTool",
             "tool_call_id": str(i), "context_version": version,
             "result": {"ok": True, "content": content, "duration_ms": i}},
        ]
    result = analyze_overhead(events)
    assert result["roles"]["codeAgent"]["tool_results"] == 4
    assert len(result["repeated_read_observations"]) == 1
    canonical = [{"type": "custom:" + e["type"], "payload": {"payload": e}} for e in events]
    assert analyze_overhead(canonical) == result
    json.dumps(result)
