"""Independent testing uses actual outcomes and the existing control boundaries."""

import json

import pytest
from langchain_core.messages import AIMessage

from mokioclaw.agents import test_agent as runner
from mokioclaw.agents.registry import default_subagents, run_subagent
from mokioclaw.core.run_control import RunControl, RunInterrupted
from mokioclaw.core.state import RuntimeState


class Scripted:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0

    def bind_tools(self, tools):
        self.names = {tool.name for tool in tools}
        return self

    def invoke(self, messages):
        self.calls += 1
        return next(self.responses)


def call(name, args, identifier="check"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": identifier}])


def test_testing_role_scope_and_no_background():
    role = default_subagents()["testAgent"]
    assert role.allowed_tools == runner.TOOLS
    assert role.max_tool_calls == 8 and not role.background_safe
    assert not role.allowed_tools & {"FileWriteTool", "FileEditTool", "MCP_*", "TodoUpdateTool"}


@pytest.mark.parametrize("passing", [True, False])
def test_real_test_command_controls_status_not_model_claim(tmp_path, monkeypatch, passing):
    (tmp_path / "test_sample.py").write_text(
        "def test_sample():\n    assert " + str(passing) + "\n"
    )
    model = Scripted(
        [
            call("BashTool", {"command": "python -m pytest -q test_sample.py"}),
            AIMessage(content="All tests passed. Task is complete."),
        ]
    )
    monkeypatch.setattr(runner, "create_model", lambda: model)
    events = []
    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto")
    result = run_subagent(
        {"runtime": runtime, "task": "test sample"}, "testAgent", "run tests", writer=events.append
    )
    report = result["state_updates"]["test_report"]
    assert report["status"] == ("passed" if passing else "failed")
    assert report["commands"][0]["exit_code"] == (0 if passing else 1)
    assert report["basis"] == "observed_tool_results"
    assert model.names == runner.TOOLS
    assert any(e["type"] == "llm_stream_end" and e["node"] == "testAgent" for e in events)
    assert any(e["type"] == "tool_result" for e in events)


def test_success_text_without_execution_is_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr(
        runner, "create_model", lambda: Scripted([AIMessage(content="All tests passed")])
    )
    result = run_subagent({"runtime": RuntimeState(workspace=tmp_path)}, "testAgent", "test")
    assert result["state_updates"]["test_report"]["status"] == "not_run"


@pytest.mark.parametrize(
    "name,args",
    [
        ("FileWriteTool", {"file_path": "app.py", "content": "bad"}),
        ("BashTool", {"command": "touch background", "run_in_background": True}),
    ],
)
def test_out_of_scope_or_background_calls_have_no_side_effect(tmp_path, monkeypatch, name, args):
    model = Scripted([call(name, args), AIMessage(content="done")])
    monkeypatch.setattr(runner, "create_model", lambda: model)
    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto")
    result = run_subagent({"runtime": runtime}, "testAgent", "test")
    assert result["state_updates"]["test_report"]["status"] == "incomplete"
    assert result["metadata"]["tool_calls_used"] == 1
    assert not (tmp_path / "app.py").exists() and not (tmp_path / "background").exists()


def test_cancel_after_generation_prevents_tool(tmp_path, monkeypatch):
    control = RunControl()

    class Cancelled(Scripted):
        def invoke(self, messages):
            control.cancel()
            return call("BashTool", {"command": "touch forbidden"})

    model = Cancelled([])
    monkeypatch.setattr(runner, "create_model", lambda: model)
    runtime = RuntimeState(workspace=tmp_path, run_control=control, approval_mode="auto")
    with pytest.raises(RunInterrupted):
        run_subagent({"runtime": runtime}, "testAgent", "test")
    assert not (tmp_path / "forbidden").exists()


def test_implementation_invalidates_old_report(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    monkeypatch.setattr(
        nodes,
        "run_subagent",
        lambda *_a, **_k: {
            "ok": True,
            "summary": "edited",
            "state_updates": {"code_agent_summary": "edited"},
        },
    )
    state = {"runtime": RuntimeState(workspace=tmp_path), "test_report": {"status": "passed"}}
    nodes._call_code_agent_tool(state, lambda _: None, "edit")
    assert state["testing_required"] and state["test_report"] == {}


@pytest.mark.parametrize("status", ["failed", "incomplete", "not_run"])
def test_verifier_cannot_overrule_failed_or_missing_required_testing(tmp_path, monkeypatch, status):
    from mokioclaw.graph import nodes

    model = Scripted([AIMessage(content=json.dumps({"passed": True, "reason": "looks fine"}))])
    monkeypatch.setattr(nodes, "create_model", lambda: model)
    result = nodes.verifier_node(
        {
            "runtime": RuntimeState(workspace=tmp_path),
            "task": "fix",
            "test_report": {"status": status},
            "verification_commands": ["pytest -q"],
        }
    )
    assert not result["passed"]
    assert any(c["name"] == "testAgent" and not c["passed"] for c in result["verification_checks"])


def test_full_flow_inserts_independent_testing_before_verifier(tmp_path, monkeypatch):
    from mokioclaw.graph import workflow

    visited = []

    def planner(_):
        visited.append("planner")
        return {"testing_required": True, "context_next_node": "verifier"}

    def testing(_):
        visited.append("testAgent")
        return {
            "testing_required": False,
            "test_report": {"status": "passed"},
            "context_next_node": "verifier",
        }

    def verifier(_):
        visited.append("verifier")
        return {"passed": True, "context_next_node": "final"}

    monkeypatch.setattr(workflow, "planner_node", planner)
    monkeypatch.setattr(workflow, "test_agent_node", testing)
    monkeypatch.setattr(workflow, "verifier_node", verifier)
    result = workflow.build_complex_workflow().invoke(
        {"runtime": RuntimeState(workspace=tmp_path), "task": "fix"}
    )
    assert result["passed"] and visited == ["planner", "testAgent", "verifier"]


def test_light_flow_tests_after_implementation(tmp_path, monkeypatch):
    from mokioclaw.graph import light

    visited = []
    monkeypatch.setattr(light, "light_executor", lambda _: visited.append("codeAgent") or {})
    monkeypatch.setattr(light, "test_agent_node", lambda _: visited.append("testAgent") or {})
    monkeypatch.setattr(
        light, "verifier_node", lambda _: visited.append("verifier") or {"passed": True}
    )
    light.build_light_workflow().invoke({"runtime": RuntimeState(workspace=tmp_path)})
    assert visited == ["codeAgent", "testAgent", "verifier"]


def test_report_survives_compaction_facts_and_resume_rechecks(tmp_path):
    from mokioclaw.core.checkpoint import (
        _copy_summary_fields,
        deserialize_state,
        serialize_state,
        state_summary,
    )
    from mokioclaw.core.context_compaction import critical_facts

    report = {"status": "failed", "commands": [{"command": "pytest", "exit_code": 1}]}
    state = {"runtime": RuntimeState(workspace=tmp_path), "test_report": report}
    assert critical_facts(state)["test_report"] == report
    restored = {"runtime": state["runtime"]}
    _copy_summary_fields(restored, state_summary(state))
    assert restored["test_report"] == report and restored["testing_required"]
    strict = deserialize_state(serialize_state({**state, "passed": True}), state["runtime"])
    assert strict["testing_required"] and not strict["passed"]


def test_drained_budget_reports_not_run_without_spending_reserved_calls(tmp_path, monkeypatch):
    from mokioclaw.core.completion_budget import CompletionBudget
    from mokioclaw.graph.steps.testing import test_agent_node as run_testing

    monkeypatch.setattr(runner, "create_model", lambda: pytest.fail("reserved model capacity"))
    runtime = RuntimeState(
        workspace=tmp_path, completion_budget=CompletionBudget(draining=True, phase="verification")
    )
    result = run_testing({"runtime": runtime, "testing_required": True})
    assert result["test_report"]["status"] == "not_run"
    assert result["test_report"]["stop_reason"] == "completion_budget_reserved"
    assert runtime.model_metrics.get("requests", 0) == 0
