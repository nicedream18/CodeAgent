"""Offline policy, fairness, accounting and recovery tests; synthetic usage is not a real benchmark."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from typer.testing import CliRunner

from mokioclaw.cli.app import app
from mokioclaw.core.context_budget import ContextBudgetError
from mokioclaw.core.context_compaction import prepare_model_context
from mokioclaw.core.context_control import ContextControl
from mokioclaw.core.settings import configure_runtime
from mokioclaw.core.state import RuntimeState
from mokioclaw.evaluation import compression_benchmark as benchmark
from mokioclaw.evaluation.compression_metrics import measure_trial
from mokioclaw.evaluation.models import EvalTrialConfig, EvalTrialResult, Grade
from mokioclaw.evaluation.worker import initialize
from mokioclaw.providers.openai_provider import invoke_streaming


class CountedModel:
    def __init__(self, content="done"):
        self.content, self.requests = content, []
        self.kwargs = {"tools": []}

    def bind(self, **_kwargs):
        return self

    def get_num_tokens_from_messages(self, messages):
        return sum(len(str(m.content)) // 4 + 4 for m in messages)

    def invoke(self, messages):
        incoming = self.get_num_tokens_from_messages(messages)
        self.requests.append(incoming)
        return AIMessage(content=self.content, usage_metadata={
            "input_tokens": incoming, "output_tokens": 4, "total_tokens": incoming + 4,
        })


def history(count=4):
    messages = [SystemMessage(content="Retain current requirements."), HumanMessage(content="Fix and verify.")]
    for i in range(count):
        messages.extend([
            AIMessage(content="", tool_calls=[{"name": "FileReadTool", "id": str(i), "args": {}}]),
            ToolMessage(content="x" * 9000, tool_call_id=str(i)),
        ])
    return messages


def runtime(path, enabled):
    return RuntimeState(workspace=path, context_token_limit=10000,
                        model_context_reserve_tokens=512, context_safety_tokens=128,
                        context_compression_enabled=enabled, context_control=ContextControl())


def test_actual_provider_ab_accounts_for_summary_and_preserves_off_history(tmp_path, monkeypatch):
    from mokioclaw.providers import openai_provider

    summarizer = CountedModel('{"summary":"Keep the task and existing evidence."}')
    monkeypatch.setattr(openai_provider, "create_model", lambda: summarizer)
    rows = {}
    for enabled in (True, False):
        state = runtime(tmp_path / str(enabled), enabled)
        model, events = CountedModel(), []
        messages = history()
        invoke_streaming(model, messages, state={"runtime": state, "task": "Fix and verify"}, writer=events.append)
        rows[enabled] = (state.model_metrics, model.requests, events)
    assert rows[True][0]["requests"] == 2 and rows[False][0]["requests"] == 1
    assert rows[True][1][0] < rows[False][1][0]
    assert len(summarizer.requests) == 1
    assert rows[True][0]["total_tokens"] == summarizer.requests[0] + rows[True][1][0] + 8
    assert any(e["type"] == "context_compression" for e in rows[True][2])
    assert not any(e["type"] == "context_compression" for e in rows[False][2])


def test_off_keeps_hard_context_limit_and_closes_manual_request(tmp_path):
    state = runtime(tmp_path, False)
    request = state.context_control.request()
    model = CountedModel()
    prepare_model_context(model, [HumanMessage(content="short")], {"runtime": state})
    result = state.context_control.get(request["request_id"])
    assert result["status"] == "failed" and result["error_type"] == "context_compression_disabled"
    with pytest.raises(ContextBudgetError, match="exceeds context budget"):
        invoke_streaming(model, history(6), state={"runtime": state})
    assert not model.requests


def test_graph_watermark_cannot_compress_disabled_run(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes
    from mokioclaw.graph.steps.compression import context_monitor_node, context_monitor_route

    monkeypatch.setattr(nodes, "_get_writer", lambda: lambda _event: None)
    monkeypatch.setattr(nodes, "estimate_context_tokens", lambda _state: 1000000)
    result = context_monitor_node({"runtime": runtime(tmp_path, False), "context_next_node": "verifier"})
    assert not result["context_should_compress"]
    assert context_monitor_route(result) == "verifier"


@pytest.mark.parametrize(("raw", "enabled"), [("false", False), ("0", False), ("true", True), ("1", True)])
def test_compression_environment_is_a_real_boolean(tmp_path, monkeypatch, raw, enabled):
    monkeypatch.setattr("mokioclaw.core.settings.load_settings", lambda _: {})
    monkeypatch.setenv("MOKIO_CONTEXT_COMPRESSION_ENABLED", raw)
    state = RuntimeState(workspace=tmp_path)
    configure_runtime(state)
    assert state.context_compression_enabled is enabled


def test_invalid_boolean_and_default_eval_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr("mokioclaw.core.settings.load_settings", lambda _: {})
    monkeypatch.setenv("MOKIO_CONTEXT_COMPRESSION_ENABLED", "sometimes")
    with pytest.raises(ValueError, match="true/false"):
        configure_runtime(RuntimeState(workspace=tmp_path))
    assert config("D01", True).context_compression_enabled
    raw = config("D01", True).model_dump()
    raw.pop("context_compression_enabled")
    assert EvalTrialConfig.model_validate(raw).context_compression_enabled


def config(task, enabled):
    return EvalTrialConfig(task_id=task, architecture="full", repeat=1,
                           context_compression_enabled=enabled, model="fixture-model",
                           model_config_sha256="fixture", harness_revision="fixture", prompt_sha256="fixture")


def fixture_trial(path, task="D01", enabled=True, solved=True):
    path.mkdir(parents=True)
    usage = {"input_tokens": 120 if enabled else 100, "output_tokens": 15 if enabled else 10,
             "total_tokens": 135 if enabled else 110, "requests": 2 if enabled else 1}
    result = EvalTrialResult(config=config(task, enabled), experiment_id="fixture", trial_id=task+"-full-1", task_sha256="fixture",
                             execution_status="finished" if solved else "failed", task_solved=solved,
                             stop_reason=None if solved else "token_budget_exhausted", run_id="r",
                             grade=Grade(valid=True, solved=True, target_passed=1, tests_run=2),
                             generation_seconds=2.0 if enabled else 3.0, model_metrics=usage)
    (path / "result.json").write_text(result.model_dump_json())
    (path / "worker.json").write_text(json.dumps({"context_compression_enabled": enabled}))
    events = [{"type": "context_budget", "compression_enabled": enabled}]
    for index in range(usage["requests"]):
        node = "context_compressor" if index else "codeAgent"
        sid = f"s{index}"
        events.extend([
            {"type": "llm_request", "stream_id": sid, "node": node,
             "model_config": {"model": "fixture-model", "context_tokens": 32768,
                              "output_tokens": 4096, "sdk_max_retries": 0}},
            {"type": "llm_stream_end", "stream_id": sid, "status": "finished"},
            {"type": "model_usage", "stream_id": sid, "agent": "codeAgent",
             "input_tokens": 20 if index else 100, "output_tokens": 5 if index else 10,
             "total_tokens": 25 if index else 110, "reported": True, "estimated": False,
             "cache_read_tokens": 0, "cache_creation_tokens": None, "duration_ms": 10},
        ])
    if enabled:
        event = {"type": "context_compression", "event_id": "c", "status": "applied"}
        events.extend([event, event])
    trace = path / "workspace/.mokioclaw/traces/r/events.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text("".join(json.dumps({"type": "custom:"+e["type"], "payload": {"payload": e}})+"\n" for e in events))
    return trace


def test_canonical_measurement_includes_compressor_not_double_counted(tmp_path):
    fixture_trial(tmp_path / "trial")
    row = measure_trial(tmp_path / "trial", True)
    assert row["measurement_valid"] and row["total_tokens"] == 135
    assert row["compression_tokens"] == 25 and row["compression_requests"] == 1
    assert row["compression_applied"] == 1
    assert row["cache_creation_tokens"]["known_sum"] is None


def test_bad_stream_or_disabled_compression_invalidates_measurement(tmp_path):
    trace = fixture_trial(tmp_path / "trial")
    text = trace.read_text()
    trace.write_text(text + next(line for line in text.splitlines() if 'custom:model_usage' in line) + "\n")
    assert not measure_trial(tmp_path / "trial", True)["measurement_valid"]
    assert not measure_trial(tmp_path / "trial", False)["measurement_valid"]


def test_paired_schedule_and_preview_do_not_call_models_or_docker(tmp_path, monkeypatch):
    assert len(benchmark.schedule()) == 18 and benchmark.schedule() == benchmark.schedule()
    for index in range(0, 18, 2):
        pair = benchmark.schedule()[index:index+2]
        assert pair[0]["task_id"] == pair[1]["task_id"] and {p["enabled"] for p in pair} == {True, False}
    for task in benchmark.TASKS:
        assert sum(p["enabled"] for p in benchmark.schedule()[::2] if p["task_id"] == task) in {1, 2}
    monkeypatch.setattr(benchmark, "_manifest", lambda *_: pytest.fail("Preview cannot load real configuration"))
    result = CliRunner().invoke(app, ["eval", "compression", "run", "fixture", "--cache", str(tmp_path)])
    assert result.exit_code == 0 and json.loads(result.stdout)["mode"] == "preview"
    assert not list(tmp_path.iterdir())


def test_report_keeps_failed_artifact_in_fixed_denominator_and_unknown_cache(tmp_path):
    trials = []
    for task in benchmark.TASKS:
        for enabled in (True, False):
            arm = "on" if enabled else "off"
            item = {"enabled": enabled, "arm": arm, "experiment_id": "fixture-"+arm,
                    "config": config(task, enabled).model_dump()}
            fixture_trial(benchmark.trial_directory(tmp_path, item), task, enabled, solved=not enabled)
            trials.append(item)
    root = tmp_path / "benchmarks/fixture"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"trials": trials, "repeats": 1, "harness_revision": "fixture"}))
    report = benchmark.report(tmp_path, "fixture")
    assert report["complete"] and report["measurement_valid"]
    assert report["arms"]["on"]["completion_rate"] == 0
    assert report["arms"]["off"]["completion_rate"] == 1
    assert report["arms"]["on"]["artifact_passed"] == 3
    assert report["arms"]["on"]["total_tokens"] == 405
    assert report["arms"]["on"]["cache_creation_tokens"]["known_sum"] is None
    assert len(report["pairs"]) == 3


def test_started_attempt_is_not_rerun(tmp_path, monkeypatch):
    item = {"enabled": True, "arm": "on", "experiment_id": "fixture-on", "config": config("D01", True).model_dump()}
    root = tmp_path / "benchmarks/fixture"
    root.mkdir(parents=True)
    manifest = {"repeats": 3, "harness_revision": "fixture", "harness_source_sha256": "source", "trials": [item]}
    (root / "manifest.json").write_text(json.dumps(manifest))
    directory = benchmark.trial_directory(tmp_path, item)
    directory.mkdir(parents=True)
    receipt = directory / "result.json"
    receipt.write_text('{"execution_status":"running"}')
    monkeypatch.setattr(benchmark, "load_dotenv", lambda *_: None)
    monkeypatch.setattr(benchmark, "_source_contract", lambda _: {"harness_revision": "fixture", "harness_source_sha256": "source"})
    monkeypatch.setattr(benchmark, "run_trial", lambda *_: pytest.fail("Must not restart"))
    with pytest.raises(ValueError, match="never automatically rerun"):
        benchmark.run(tmp_path, tmp_path, "fixture", real_model=True)
    assert receipt.read_text() == '{"execution_status":"running"}'


def test_evaluation_initializer_pins_compression_policy(tmp_path):
    from mokioclaw.core.run_control import RunControl

    control = RunControl()
    state = RuntimeState(workspace=tmp_path, run_control=control)
    initialize(state, config("D01", False), "sha256:fixture", "fixture-owner")
    assert not state.context_compression_enabled
    assert state.context_token_limit == 32768 and state.settings["model_profiles"]["evaluation"]["max_retries"] == 0
    control.close()


def test_missing_canonical_usage_preserves_receipt_tokens_without_calling_it_valid(tmp_path):
    trace = fixture_trial(tmp_path / "trial")
    trace.unlink()
    row = measure_trial(tmp_path / "trial", True)
    assert not row["measurement_valid"] and row["total_tokens"] == 135
    assert row["reported_total_tokens"] == 0 and row["usage_missing_requests"] == 2


def test_unfinished_report_has_no_finished_completion_rate(tmp_path):
    item = {"enabled": True, "arm": "on", "experiment_id": "fixture-on", "config": config("D01", True).model_dump()}
    root = tmp_path / "benchmarks/fixture"
    root.mkdir(parents=True)
    (root / "manifest.json").write_text(json.dumps({"trials": [item], "repeats": 1, "harness_revision": "fixture"}))
    report = benchmark.report(tmp_path, "fixture")
    assert not report["complete"] and report["arms"]["on"]["completion_rate"] is None
    assert len(report["pending"]) == 1 and not report["pending"][0]["started"]
