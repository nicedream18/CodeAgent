import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core.completion_budget import CompletionBudget, CompletionYield, report_phase
from mokioclaw.core.run_control import RunInterrupted
from mokioclaw.core.run_inbox import RunRedirected
from mokioclaw.core.state import RuntimeState
from mokioclaw.providers.model_policy import RunBudgetExceeded, begin_request

spec = spec_from_file_location("offline_completion_demo", Path(__file__).resolve().parents[1]
                               / "examples/completion_budget_demo.py")
assert spec is not None and spec.loader is not None
demo = module_from_spec(spec)
spec.loader.exec_module(demo)
Code, reply, run_demo = demo.Code, demo.reply, demo.run_demo


@pytest.mark.parametrize("kind,trigger", [
    ("requests", "request_headroom"), ("tokens", "token_headroom"), ("tools", "tool_headroom"),
])
def test_full_workflow_yields_synchronous_child_closes_batch_and_reaches_verifier(kind, trigger):
    result = run_demo(kind)
    assert not result["real_model"] and result["execution_status"] == "finished"
    assert result["passed"] and result["file_content"] == "fixed\n"
    assert result["completion_budget"]["trigger"] == trigger
    assert result["requests"] <= (10 if kind == "requests" else 40)
    assert result["tools"] <= (7 if kind == "tools" else 80)
    if kind == "tokens":
        assert result["total_tokens"] <= 1800
    assert result["roles"]["verifier"]["requests"] == 2
    handoffs = [e for e in result["events"] if e["type"] == "handoff"]
    assert len(handoffs) == 1
    discarded = next(e for e in result["events"] if e.get("tool_call_id") == "obsolete")
    assert discarded["result"]["status"] == "not_executed"
    requests = [e for e in result["events"] if e["type"] == "llm_request"]
    ends = [e for e in result["events"] if e["type"] == "llm_stream_end"]
    assert len(requests) == len(ends) == result["requests"]
    assert {e["stream_id"] for e in requests} == {e["stream_id"] for e in ends}


def test_failed_reserved_verification_stops_without_redelegation_or_synthetic_success():
    result = run_demo(passed=False)
    assert result["execution_status"] == "failed" and not result["passed"]
    assert result["stop_reason"] == "completion_verification_failed"
    assert result["file_content"] == "fixed\n"
    assert len([e for e in result["events"] if e["type"] == "handoff"]) == 1


@pytest.mark.parametrize("action,exception", [("cancel", RunInterrupted), ("correct", RunRedirected)])
def test_cancel_or_correction_wins_before_reserved_report(action, exception):
    observed = []

    def interrupt(event, runtime):
        if event["type"] == "completion_budget":
            observed.append((runtime.workspace / "a.py").read_text())
            if action == "cancel":
                runtime.run_control.cancel()
            else:
                runtime.run_control.inbox.submit("correction", "Do not change any other files")

    with pytest.raises(exception):
        run_demo(on_event=interrupt)
    assert observed == ["fixed\n"]


def test_unexpected_report_tools_are_closed_without_side_effect(monkeypatch):
    monkeypatch.setattr(Code, "invoke", lambda *_: reply(calls=[{
        "name": "FileWriteTool", "args": {"file_path": "late.py", "content": "bad"}, "id": "late",
    }]))
    result = run_demo()
    assert "late.py" not in result["workspace_files"]
    rejected = next(e for e in result["events"] if e.get("tool_call_id") == "late")
    assert rejected["result"]["status"] == "not_executed"
    assert result["completion_budget"]["draining"]


@pytest.mark.parametrize("phase", ["report", "verification"])
def test_protected_phases_still_obey_existing_hard_request_budget(tmp_path, phase):
    runtime = RuntimeState(workspace=tmp_path, run_request_limit=1,
                           completion_budget=CompletionBudget(phase=phase))
    runtime.model_metrics = {"requests": 1}
    with pytest.raises(RunBudgetExceeded):
        begin_request(runtime, "codeAgent", SimpleNamespace(input_tokens=1), SimpleNamespace(output=1))
    assert runtime.model_metrics["requests"] == 1


def test_report_scope_restores_phase_on_failure(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, completion_budget=CompletionBudget())
    with pytest.raises(ValueError), report_phase(runtime):
        assert runtime.completion_budget.phase == "report"
        raise ValueError("controlled")
    assert runtime.completion_budget.phase == "implementation"


def test_retry_cannot_spend_reserved_tool_capacity(tmp_path):
    from mokioclaw.core.tool_loop import ToolConversation
    from mokioclaw.providers.openai_provider import invoke_streaming
    from mokioclaw.tools.execution import execute_tool

    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto", run_tool_limit=3,
                           completion_budget=CompletionBudget())
    calls = []

    def flaky(file_path: str):
        calls.append(file_path)
        return {"ok": False, "error_type": "transient", "retryable": True}

    tool = StructuredTool.from_function(flaky, name="FileReadTool", description="retry fixture")
    messages = [HumanMessage(content="inspect"), AIMessage(content="", tool_calls=[
        {"name": "FileReadTool", "args": {"file_path": "a.py"}, "id": "read"},
    ])]
    events = []
    conversation = ToolConversation(messages, state={"runtime": runtime}, writer=events.append,
                                    node="codeAgent", invoke=invoke_streaming)
    with conversation.batch(messages[-1].tool_calls) as pending:
        for call in pending:
            execute_tool(runtime, tool, call["args"], on_event=events.append)
    assert calls == ["a.py"] and runtime.tool_execution_count == 1
    assert conversation.yielded and runtime.completion_budget.draining
    result = next(e for e in events if e["type"] == "tool_result")
    assert result["result"]["status"] == "interrupted"


def test_completion_compaction_is_deterministic_and_keeps_facts(tmp_path):
    from langchain_core.messages import ToolMessage

    from mokioclaw.core.context_budget import ContextBudget
    from mokioclaw.core.context_compaction import compact_messages

    runtime = RuntimeState(workspace=tmp_path)
    messages = [HumanMessage(content="immutable task"), AIMessage(content="", tool_calls=[
        {"name": "FileReadTool", "args": {}, "id": "read"},
    ]), ToolMessage(content="observed source", tool_call_id="read")]

    def reserved(*args):
        raise CompletionYield("completion_uses_deterministic_compaction")

    replacement, event = compact_messages(
        None, messages, {"runtime": runtime, "task": "immutable task", "acceptance_criteria": ["check"]},
        budget=ContextBudget(window=8192, output=128), summarizer=reserved,
        reason="manual",
    )
    assert event["degraded"]
    facts = next(json.loads(m.content)["facts"] for m in replacement
                 if isinstance(m, AIMessage) and not m.tool_calls)
    assert facts["task"] == "immutable task" and facts["acceptance_criteria"] == ["check"]


@pytest.mark.parametrize("mode,passed", [("light", True), ("strict", False)])
def test_real_stream_lifecycle_saves_completion_phase_and_terminal_trace(
    tmp_path, monkeypatch, mode, passed
):
    from mokioclaw.agents import code_agent, repo_agent
    from mokioclaw.core import agent
    from mokioclaw.core.run_control import RunControl
    from mokioclaw.graph import nodes

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setattr(nodes, "create_model", lambda: demo.Coordinator(passed))
    monkeypatch.setattr(code_agent, "create_model", demo.Code)
    monkeypatch.setattr(repo_agent, "create_model", demo.Repo)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.py").write_text("bad\n")
    control = RunControl()

    def initialize(runtime):
        runtime.run_request_limit = 10
        runtime.run_tool_limit = 80
        runtime.context_token_limit = 8192
        runtime.model_context_reserve_tokens = 128

    control.runtime_initializer = initialize
    try:
        events = list(agent.stream_agent_events(
            "Change bad to fixed in a.py", workspace=workspace, run_control=control,
            approval_mode="auto", checkpoint_mode=mode, workflow_mode="full",
        ))
    finally:
        control.close()
    checkpoint = json.loads((workspace / ".mokioclaw/checkpoints/checkpoint.json").read_text())
    assert checkpoint["status"] == ("finished" if passed else "failed")
    assert checkpoint["summary"]["completion_budget"]["draining"]
    assert checkpoint["summary"]["passed"] is passed
    assert (workspace / "a.py").read_text() == "fixed\n"
    custom = [e["event"] for e in events if isinstance(e.get("event"), dict)]
    finished = [e for e in custom if e.get("type") == "run_finished"]
    assert len(finished) == 1 and finished[0]["status"] == checkpoint["status"]
    traces = list((workspace / ".mokioclaw/traces").glob("*/summary.json"))
    assert len(traces) == 1
    trace = json.loads(traces[0].read_text())
    assert trace["status"] == checkpoint["status"]
    assert trace["model_metrics"]["roles"]["verifier"]["requests"] == 2
    if not passed:
        assert checkpoint["summary"]["stop_reason"] == "completion_verification_failed"
        stopped = next(e for e in custom if e.get("type") == "run_budget_exhausted")
        assert "预留验收阶段" in stopped["message"]


def test_restore_recomputes_reservation_instead_of_reusing_old_drain(tmp_path):
    from mokioclaw.core.checkpoint import _copy_summary_fields, state_summary
    from mokioclaw.core.completion_budget import activate_completion_budget

    old = RuntimeState(workspace=tmp_path, run_request_limit=10,
                       completion_budget=CompletionBudget(phase="verification", draining=True))
    summary = state_summary({"runtime": old, "task": "resume", "passed": True})
    restored = RuntimeState(workspace=tmp_path, run_request_limit=10)
    state = {"runtime": restored, "task": "resume"}
    _copy_summary_fields(state, summary)
    assert restored.completion_budget is None
    control = activate_completion_budget(restored)
    assert control is not None and not control.draining and control.input_estimate == 0
    assert "passed" not in state


def test_drained_child_report_does_not_spend_model_capacity_on_compaction(tmp_path):
    from mokioclaw.providers.openai_provider import invoke_streaming

    class ForbiddenModel(demo.Counter):
        def invoke(self, messages):
            pytest.fail("No compressor model may run in reserved completion")

    runtime = RuntimeState(workspace=tmp_path, run_request_limit=10,
                           active_agent_name="codeAgent",
                           completion_budget=CompletionBudget(phase="report", draining=True))
    with pytest.raises(CompletionYield):
        invoke_streaming(ForbiddenModel(), [HumanMessage(content="observations")],
                         state={"runtime": runtime}, node="context_compressor", allow_compaction=False)
    assert runtime.model_metrics.get("requests", 0) == 0
