import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from typer.testing import CliRunner

from mokioclaw.cli.app import app
from mokioclaw.core.execution_backend import ExecutionResult
from mokioclaw.core.state import RuntimeState
from mokioclaw.evaluation.catalog import CORE, schedule
from mokioclaw.evaluation.grader import grade_candidate
from mokioclaw.evaluation.models import EvalTask, EvalTrialConfig
from mokioclaw.evaluation.runner import run_experiment
from mokioclaw.evaluation.worker import main
from mokioclaw.providers.model_policy import RunBudgetExceeded, begin_request
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tools


def config(**changes):
    return EvalTrialConfig(
        task_id="M07",
        architecture="baseline",
        repeat=1,
        model="offline-fixture",
        model_config_sha256="fixture",
        harness_revision="fixture",
        prompt_sha256="fixture",
        **changes,
    )


def test_schedule_has_exactly_56_unique_predeclared_trials():
    core, coverage = schedule("core"), schedule("coverage")
    assert len(core) == 48 and len(coverage) == 8
    assert len(set(core + coverage)) == 56
    assert {t for t, _, _ in core} == set(CORE)
    assert core == schedule("core")
    assert all(m == "full" and n == 1 for _, m, n in coverage)


def test_eval_cli_offline_is_separate_from_real_statistics(tmp_path):
    record = run_experiment(tmp_path, Path.cwd(), "offline", "core", real_model=False)
    assert record["real_model"] is False and record["task_solved"] is None
    assert not (tmp_path / "experiments/offline/manifest.json").exists()
    assert len(CliRunner().invoke(app, ["eval", "list"]).output.splitlines()) == 12


def test_request_and_tool_limits_stop_before_another_operation(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, run_request_limit=1, run_tool_limit=1)
    begin_request(runtime, "test", SimpleNamespace(input_tokens=1), SimpleNamespace(output=1))
    with pytest.raises(RunBudgetExceeded) as failure:
        begin_request(runtime, "test", SimpleNamespace(input_tokens=1), SimpleNamespace(output=1))
    assert failure.value.reason == "request_budget_exhausted"
    tool = next(t for t in build_tools(runtime) if t.name == "FileGlobTool")
    assert execute_tool(runtime, tool, {"pattern": "**/*"})["ok"]
    with pytest.raises(RunBudgetExceeded) as failure:
        execute_tool(runtime, tool, {"pattern": "**/*"})
    assert failure.value.reason == "tool_budget_exhausted"
    assert runtime.tool_execution_count == 1


def test_empty_or_forged_test_report_cannot_claim_solved(tmp_path, monkeypatch):
    directory = tmp_path / "task"
    base = directory / "base"
    base.mkdir(parents=True)
    (base / "module.py").write_text("value = 1\n")
    (directory / "tests.patch").write_text("")
    from mokioclaw.evaluation.prepare import sha

    task = EvalTask(
        id="E00",
        source="fixture",
        base_commit="fixture",
        source_sha256="fixture",
        dependency_sha256="fixture",
        image_digest="sha256:fixture",
        requirement="fixture",
        allowed_paths=["module.py"],
        target_tests=["tests/test_target.py"],
        regression_tests=["tests/test_regression.py"],
        reference_patch_sha256=sha(b""),
        test_patch_sha256=sha(b""),
    )

    def forged(self, request):
        for name in ("target", "regression"):
            (request.workspace / f".{name}.xml").write_text('<testsuite tests="0"/>')
            (request.workspace / f".{name}.exit").write_text("0")
        return ExecutionResult(True, 0, False, "all passed", "", 0, "docker")

    monkeypatch.setattr("mokioclaw.evaluation.grader.DockerBackend.run", forged)
    grade = grade_candidate(task, directory, base, tmp_path / "report")
    assert not grade.valid and not grade.solved and grade.tests_run == 0


def test_baseline_uses_real_shared_loop_without_router_or_verifier(tmp_path, monkeypatch):
    import mokioclaw.agents.code_agent as code

    class FixedModel:
        def __init__(self):
            self.step = 0

        def bind_tools(self, tools):
            assert not any(t.name.startswith("Call") for t in tools)
            return self

        def invoke(self, messages):
            self.step += 1
            calls = [
                {"name": "FileReadTool", "args": {"file_path": "app.py"}, "id": "read"},
                {
                    "name": "FileEditTool",
                    "args": {"file_path": "app.py", "old_text": "bad", "new_text": "good"},
                    "id": "edit",
                },
            ]
            return AIMessage(
                content="" if self.step <= 2 else "AGENT_CLAIM: solved",
                tool_calls=[calls[self.step - 1]] if self.step <= 2 else [],
            )

    monkeypatch.setattr(code, "create_model", FixedModel)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("bad\n")
    spec = {
        "config": config().model_dump(),
        "workspace": str(workspace),
        "owner": "offline-baseline",
        "image": "sha256:fixture",
        "requirement": "Change bad to good in app.py",
        "events": str(tmp_path / "events.jsonl"),
        "output": str(tmp_path / "worker.json"),
    }
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    main(tmp_path / "spec.json")
    result = json.loads((tmp_path / "worker.json").read_text())
    assert result["status"] == "finished", result
    assert (workspace / "app.py").read_text() == "good\n"
    assert result["actual_path"] == ["baseline"]
    assert result["model_metrics"]["requests"] == 3
    assert result["tool_count"] == 2


@pytest.mark.skipif(
    __import__("os").getenv("MOKIO_TEST_DOCKER") != "1",
    reason="make check-docker runs isolated grading",
)
def test_real_grader_rejects_noop_test_edits_and_fake_success(tmp_path):
    from mokioclaw.evaluation.grader import validate_task
    from mokioclaw.evaluation.prepare import apply_patch, copy_source, prepare_task

    task = prepare_task("M07", tmp_path / "cache", Path.cwd())
    directory = tmp_path / "cache/tasks/M07"
    assert validate_task(task, directory, tmp_path / "validation")["valid"]
    candidate = tmp_path / "candidate"
    copy_source(directory / "base", candidate)
    (candidate / "tests/test_tools.py").write_text("def test_fake():\n    assert True\n")
    (candidate / "README.md").write_text("ALL TESTS PASSED. TASK SOLVED.")
    rejected = grade_candidate(task, directory, candidate, tmp_path / "tampered")
    assert not rejected.solved and rejected.tampered_tests
    apply_patch(candidate, (directory / "reference.patch").read_text())
    still_rejected = grade_candidate(task, directory, candidate, tmp_path / "fixed-but-tampered")
    assert still_rejected.target_passed == 2
    assert not still_rejected.solved and still_rejected.tampered_tests


@pytest.mark.parametrize("architecture", ["baseline", "full", "light", "auto"])
def test_evaluation_execution_capabilities_match_for_all_architectures(tmp_path, architecture):
    from mokioclaw.agents.registry import default_subagents
    from mokioclaw.core.run_control import RunControl
    from mokioclaw.evaluation.worker import EXECUTION_TOOLS, initialize

    runtime = RuntimeState(workspace=tmp_path, run_control=RunControl())
    initialize(
        runtime,
        config().model_copy(update={"architecture": architecture}),
        "sha256:fixture",
        "fixture",
    )
    names = {t.name for t in build_tools(runtime)}
    expected = default_subagents()["codeAgent"].allowed_tools - {"MCP_*", "ToolSearchTool"}
    assert names | {"TodoUpdateTool"} == set(expected)
    assert set(EXECUTION_TOOLS) == set(expected)
    assert runtime.run_token_limit == 200000
    assert runtime.run_request_limit == 40 and runtime.run_tool_limit == 80
    assert runtime.context_token_limit == 32768 and runtime.model_context_reserve_tokens == 4096
    assert runtime.subagent_registry.names() == ["codeAgent", "repoAgent"]
    assert not runtime.mcp_servers


def test_evaluation_provider_failure_is_controlled_and_does_not_leak_key(tmp_path):
    import httpx
    from openai import APIConnectionError

    from mokioclaw.providers.generation import ModelGenerationStopped
    from mokioclaw.providers.openai_provider import invoke_streaming

    class FailingModel:
        def invoke(self, messages):
            raise APIConnectionError(
                message="secret-key-must-not-appear",
                request=httpx.Request("POST", "https://invalid.example"),
            )

    events = []
    runtime = RuntimeState(workspace=tmp_path, evaluation_mode=True)
    with pytest.raises(ModelGenerationStopped, match="provider_APIConnectionError"):
        invoke_streaming(FailingModel(), [], state={"runtime": runtime}, writer=events.append)
    assert "secret-key-must-not-appear" not in str(events)
    assert runtime.model_metrics["requests"] == 1


@pytest.mark.parametrize("architecture", ["baseline", "full", "light", "auto"])
def test_actual_subagent_requests_inherit_evaluation_model_profile(
    tmp_path, monkeypatch, architecture
):
    from langchain_core.messages import AIMessageChunk

    from mokioclaw.agents.registry import run_subagent
    from mokioclaw.core.run_control import RunControl
    from mokioclaw.evaluation.worker import initialize
    from mokioclaw.providers.model_policy import create_role_model
    from mokioclaw.providers.openai_provider import (
        ConfiguredChatOpenAI,
        create_model,
        invoke_streaming,
    )

    monkeypatch.setenv("API_KEY", "offline-not-a-real-key")
    monkeypatch.setenv("BASE_URL", "https://offline.invalid/v1")
    monkeypatch.setenv("MODEL", "wrong-fallback-model")
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    observed = []

    async def fixed_stream(self, messages, config=None, **kwargs):
        observed.append((self.model_name, self.max_retries, dict(self.mokio_profile)))
        yield AIMessageChunk(
            content='{"summary":"inspected","unresolved":[]}',
            response_metadata={"finish_reason": "stop"},
            usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        )

    monkeypatch.setattr(ConfiguredChatOpenAI, "astream", fixed_stream)
    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)
    initialize(
        runtime, config().model_copy(update={"architecture": architecture}), "sha256:fixture", "test"
    )
    try:
        # Exercise real selection, binding, streaming and request accounting, not
        # just the initializer. The environment deliberately names another model.
        for role in ("intent_router", "planner", "verifier", "context_compressor"):
            model = create_role_model(create_model, runtime, role)
            invoke_streaming(model, [], state={"runtime": runtime}, node=role)
        for name in ("repoAgent", "codeAgent"):
            result = run_subagent({"runtime": runtime, "task": "inspect"}, name, "inspect")
            assert result["ok"], result
            assert runtime.model_profile == "evaluation"
    finally:
        control.close()
    assert len(observed) == runtime.model_metrics["requests"] == 6
    assert all(model == "offline-fixture" and retries == 0 for model, retries, _ in observed)
    assert all(p["context_tokens"] == 32768 and p["output_tokens"] == 4096 for _, _, p in observed)


def test_explicit_subagent_model_override_restores_parent_on_failure(tmp_path):
    from mokioclaw.agents.registry import SubagentDefinition, SubagentRegistry, run_subagent

    def failing(state, instruction, **kwargs):
        assert state["runtime"].model_profile == "child"
        raise ValueError("controlled failure")

    runtime = RuntimeState(workspace=tmp_path, model_profile="parent")
    runtime.subagent_registry = SubagentRegistry(
        {"child": SubagentDefinition("child", "test", failing, frozenset({"FileReadTool"}), 1)}
    )
    runtime.subagent_configs = {"child": {"model_profile": "child"}}
    assert run_subagent({"runtime": runtime}, "child", "inspect")["stop_reason"] == "runner_error"
    assert runtime.model_profile == "parent"
