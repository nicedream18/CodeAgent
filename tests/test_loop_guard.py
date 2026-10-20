from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core.checkpoint import CheckpointManager, load_resume_inputs
from mokioclaw.core.loop_guard import (
    LoopDecision,
    LoopGuard,
    LoopRecovery,
    guard_for,
    prepare_request,
    sync_guard,
)
from mokioclaw.core.run_control import RunControl, RunInterrupted
from mokioclaw.core.run_inbox import RunRedirected, tool_call_batch
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.tools.execution import execute_tool


def runtime(tmp_path):
    return RuntimeState(workspace=tmp_path, approval_mode="allow", run_control=RunControl())


def test_fingerprints_preserve_arrays_and_shell_semantics():
    g = LoopGuard()
    f = g.call_fingerprint
    assert f("BashTool", {"command": " x  y "}, "local") == f(
        "BashTool", {"command": "x  y"}, "local"
    )
    assert f("BashTool", {"command": "x y"}, "local") != f("BashTool", {"command": "x  y"}, "local")
    assert f("a", {"a": [1, 2]}, "local") != f("a", {"a": [2, 1]}, "local")
    assert f("a", {"b": 1, "a": 2}, "x") == f("a", {"a": 2, "b": 1}, "x")
    assert f("a", {}, "docker-image1") != f("a", {}, "docker-image2")


def test_three_failures_warn_fourth_prevented_and_state_private(tmp_path):
    rt = runtime(tmp_path)
    invoked = []

    def fail(secret: str):
        invoked.append(secret)
        return {"ok": False, "error_type": "invalid_input", "error": "bad value"}

    tool = StructuredTool.from_function(fail, name="FileReadTool", description="fail")
    for i in range(3):
        assert not execute_tool(rt, tool, {"secret": "sensitive-value"}, tool_call_id=str(i))["ok"]
    assert guard_for(rt).warning == "repeated_failure"
    with pytest.raises(LoopRecovery, match="failed_call_loop"):
        execute_tool(rt, tool, {"secret": "sensitive-value"}, tool_call_id="four")
    assert len(invoked) == 3
    assert "sensitive-value" not in json.dumps(guard_for(rt).snapshot())


def test_successful_reads_are_advisory_and_ranges_are_distinct():
    g = LoopGuard()
    for _ in range(4):
        assert g.before_call("read") is None
        g.observe("read", "FileReadTool", {"ok": True, "path": "a.py", "content": "1: x"})
    assert g.warning == "repeated_success"
    before = g.epoch
    g.observe("other-range", "FileReadTool", {"ok": True, "content": "2: y"})
    assert g.epoch > before


def test_sequence_requires_distinct_calls_and_stable_results():
    g = LoopGuard()
    for index, call in enumerate(["a", "b"] * 3):
        g.observe(call, "Other", {"ok": True, "duration_ms": 123, "call_id": call, "container_id": str(index), "stdout_full_output_path": f"stdout-{index}.log",
                "metadata": {"stderr_full_output_path": f"stderr-{index}.log"}})
    assert g.pending and g.pending.reason == "repeated_sequence"
    single = LoopGuard()
    for _ in range(12):
        single.observe("read", "Other", {"ok": True})
    assert not single.pending


def test_rounds_require_repetition_and_verifier_failure_is_not_prose_progress():
    g = LoopGuard()
    for _ in range(6):
        assert g.model_round() is None
    assert g.model_round() is None  # empty prose alone cannot manufacture a loop
    g.observe("same", "Other", {"ok": True})
    g.observe("same", "Other", {"ok": True})
    assert g.model_round().reason == "no_progress"
    g = LoopGuard()
    g.verification([{"passed": False, "check": "test_x"}], False)
    for _ in range(6):
        g.model_round()
    g.verification([{"passed": False, "check": "test_x"}], False)
    assert g.model_round().reason == "no_progress"


def test_workspace_change_allows_failure_retest_but_todo_does_not(tmp_path):
    rt = runtime(tmp_path)
    (tmp_path / "x.py").write_text("broken")
    g = sync_guard(rt)
    f = g.call_fingerprint("BashTool", {"command": "pytest"}, "local")
    for _ in range(3):
        g.observe(f, "BashTool", {"ok": False, "exit_code": 1, "error_type": "tool_error"})
    assert g.before_call(f)
    (tmp_path / "TODO.md").write_text("I am done")
    assert sync_guard(rt).before_call(f)
    (tmp_path / "x.py").write_text("fixed")
    assert sync_guard(rt).before_call(f) is None


def test_context_compression_and_role_switch_preserve_history_credit(tmp_path):
    rt = runtime(tmp_path)
    g = sync_guard(rt)
    g.observe("a", "Other", {"ok": False})
    assert g.recover()
    snapshot = g.snapshot()
    rt.context_version += 1
    rt.active_agent_name = "verifier"
    assert sync_guard(rt).snapshot() == snapshot
    assert not g.recover()


@pytest.mark.parametrize("mode", ["light", "strict"])
def test_checkpoint_recovery_credit_survives(tmp_path, mode):
    rt = runtime(tmp_path)
    rt.checkpoint_mode = mode
    g = sync_guard(rt)
    g.recover()
    g.observe("a" * 64, "Other", {"ok": False})
    CheckpointManager(rt).save({"task": "fix", "runtime": rt, "messages": []})
    resumed = runtime(tmp_path)
    resumed.checkpoint_mode = mode
    load_resume_inputs(resumed)
    assert guard_for(resumed).recoveries == 1
    assert len(guard_for(resumed).history) == 1
    assert not guard_for(resumed).recover()


def test_batch_closes_only_unanswered_calls():
    calls = [{"id": str(i), "name": "X"} for i in range(3)]
    messages = [AIMessage(content="", tool_calls=[{**c, "args": {}} for c in calls])]
    events = []
    with pytest.raises(LoopRecovery):
        with tool_call_batch(calls, messages, events.append, "codeAgent") as pending:
            for c in pending:
                if c["id"] == "1":
                    raise LoopRecovery(LoopDecision("recover", "failed_call_loop"))
                messages.append(ToolMessage(content="done", tool_call_id=c["id"]))
    results = [m for m in messages if isinstance(m, ToolMessage)]
    assert [m.tool_call_id for m in results] == ["0", "1", "2"]
    assert all(json.loads(m.content)["status"] == "not_executed" for m in results[1:])


def test_cancel_and_correction_take_priority(tmp_path):
    rt = runtime(tmp_path)
    g = sync_guard(rt)
    g.pending = LoopDecision("recover", "no_progress")
    rt.run_control.cancel()
    with pytest.raises(RunInterrupted):
        prepare_request(rt, [], None, "planner")
    rt.run_control.cancel_event.clear()
    rt.run_control.inbox.submit("new", "do not write")
    with pytest.raises(RunRedirected):
        prepare_request(rt, [], None, "planner")


def test_warning_does_not_request_extra_model_and_is_bounded(tmp_path):
    rt = runtime(tmp_path)
    g = sync_guard(rt)
    g.warning = "repeated_success"
    messages, seen = [HumanMessage(content="fix")], []
    convo = ToolConversation(
        messages,
        state={"runtime": rt},
        writer=lambda e: None,
        node="codeAgent",
        invoke=lambda *a, **kw: seen.append(1) or AIMessage(content="ok"),
    )
    for _ in range(3):
        convo.request(None)
    assert len(seen) == 3
    assert sum(str(m.content).startswith("MOKIO LOOP GUARD") for m in messages) == 1


def test_history_and_evidence_bounded_and_roundtrip():
    g = LoopGuard()
    for i in range(500):
        g.observe(str(i), "FileReadTool", {"ok": True, "content": str(i)})
    assert len(g.history) == len(g.evidence) == 128
    restored = LoopGuard.restore(g.snapshot())
    assert restored.recoveries == 0
    assert len(restored.evidence) == 128


def test_real_flow_unwinds_child_replans_once_then_fails_with_checkpoint(tmp_path, monkeypatch):
    from mokioclaw.agents import code_agent, repo_agent
    from mokioclaw.core.agent import stream_agent_events
    from mokioclaw.graph import nodes

    calls, models = [], []

    class Coordinator:
        def __init__(self):
            self.planner = False

        def bind_tools(self, tools):
            self.planner = any(t.name == "CallCodeAgentTool" for t in tools)
            return self

        def invoke(self, messages):
            if not self.planner:
                return AIMessage(content='{"route":"workflow","confidence":1}')
            models.append(1)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "CallCodeAgentTool",
                        "args": {"instruction": "inspect missing file"},
                        "id": f"child-{len(models)}",
                    },
                    {
                        "name": "TodoWriteTool",
                        "args": {
                            "todos": ["OBSOLETE"],
                            "acceptance_criteria": [],
                            "verification_commands": [],
                        },
                        "id": f"late-{len(models)}",
                    },
                ],
            )

    class Failure:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            calls.append(1)
            if len(calls) == 1:
                return AIMessage(content="", tool_calls=[{"name":"FileWriteTool",
                    "args":{"file_path":"effect.txt","content":"preserved"},"id":"effect-write"}])
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileReadTool",
                        "args": {"file_path": "missing.py"},
                        "id": f"read-{len(calls)}",
                    }
                ],
            )

    class Repo:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(content='{"summary":"no relevant files"}')

    monkeypatch.setattr(nodes, "create_model", Coordinator)
    monkeypatch.setattr(code_agent, "create_model", Failure)
    monkeypatch.setattr(repo_agent, "create_model", Repo)
    rows = list(
        stream_agent_events("inspect missing file", workspace=tmp_path, approval_mode="deny")
    )
    events = [r["event"] for r in rows if r["type"] == "custom_event"]
    assert sum(e["type"] == "loop_replan" for e in events) == 1
    assert sum(e["type"] == "loop_stop" for e in events) == 1
    assert events[-1]["status"] == "failed"
    assert len(models) == 2
    assert (tmp_path/"effect.txt").read_text()=="preserved"
    assert "OBSOLETE" not in (tmp_path / "TODO.md").read_text()
    discarded = [e for e in events if e.get("tool_call_id", "").startswith("late-")]
    assert len(discarded) == 2 and all(e["result"]["status"] == "not_executed" for e in discarded)
    checkpoint = json.loads((tmp_path / ".mokioclaw/checkpoints/checkpoint.json").read_text())
    assert checkpoint["summary"]["loop_guard"]["recoveries"] == 1
    assert checkpoint["summary"]["stop_reason"] == "collaboration_loop_detected"
    assert checkpoint["status"] == "failed"
    assert checkpoint["summary"]["testing_required"] is True


def test_internal_retry_observed_once_and_transient_not_failed_loop(tmp_path, monkeypatch):
    import mokioclaw.tools.execution as execution

    rt = runtime(tmp_path)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            return {"ok": False, "error_type": "transient", "retryable": True}
        return {"ok": True, "content": "observed"}

    monkeypatch.setattr(execution.time, "sleep", lambda _: None)
    tool = StructuredTool.from_function(flaky, name="FileReadTool", description="flaky")
    assert execute_tool(rt, tool, {})["ok"]
    assert len(attempts) == 3 and len(guard_for(rt).history) == 1
    g = LoopGuard()
    for _ in range(4):
        g.observe("same", "Other", {"ok": False, "error_type": "transient"})
    assert not g.before_call("same")


def test_pending_loop_during_approval_wakes_without_executing_late_allow(tmp_path):
    rt = runtime(tmp_path)
    rt.approval_mode = "inline"
    rt.tool_policies["BashTool"] = "ask"
    effects, superseded = [], []
    rt.run_control.supersede_approvals = lambda: superseded.append(True)

    def approval(request):
        guard_for(rt).pending = LoopDecision("recover", "repeated_sequence")
        return True

    rt.approval_handler = approval
    tool = StructuredTool.from_function(
        lambda command: effects.append(1) or {"ok": True},
        name="BashTool",
        description="side effect",
    )
    with pytest.raises(LoopRecovery):
        execute_tool(rt, tool, {"command": "cat /tmp/approval-fixture"})
    assert superseded and not effects


def test_applied_user_correction_invalidates_failures_without_refunding_credit(tmp_path):
    rt = runtime(tmp_path)
    rt.foreground_state = state = {"runtime": rt, "task": "fix"}
    g = sync_guard(rt)
    g.recover()
    for _ in range(3):
        g.observe("same", "Other", {"ok": False, "error_type": "invalid_input"})
    assert g.before_call("same")
    rt.run_control.inbox.submit("correction", "use other path")
    rt.run_control.inbox.apply(state)
    assert sync_guard(rt).before_call("same") is None
    assert g.recoveries == 1


def test_background_guards_recover_independently_without_consuming_parent_credit(tmp_path):
    from mokioclaw.agents.registry import SubagentDefinition, SubagentRegistry
    from mokioclaw.core.subagent_tasks import SubagentTaskManager
    from mokioclaw.graph.nodes import _call_registered_subagent_tool

    manager = SubagentTaskManager(tmp_path / ".mokioclaw/runs", max_running=2)
    rt = runtime(tmp_path)
    rt.subagent_task_manager, rt.parent_run_id = manager, "parent"
    captured = []

    def loop(state, instruction, *, writer, max_loops):
        captured.append(guard_for(state["runtime"]))
        raise LoopRecovery(LoopDecision("recover", "failed_call_loop"))

    def good(state, instruction, *, writer, max_loops):
        return {"ok": True, "summary": "done"}

    registry = SubagentRegistry()
    registry.register(
        SubagentDefinition(
            "reader", "loop fixture", loop, frozenset({"FileReadTool"}), 8, background_safe=True
        )
    )
    registry.register(
        SubagentDefinition(
            "sibling", "healthy fixture", good, frozenset({"FileReadTool"}), 8, background_safe=True
        )
    )
    rt.subagent_registry = registry
    state = {"runtime": rt, "task": "inspect"}
    try:
        a = _call_registered_subagent_tool(
            state, lambda e: None, "reader", "inspect", run_in_background=True
        )
        b = _call_registered_subagent_tool(
            state, lambda e: None, "sibling", "inspect", run_in_background=True
        )
        assert manager.wait(a["subagent_run_id"], timeout_seconds=3)["status"] == "failed"
        assert manager.wait(b["subagent_run_id"], timeout_seconds=3)["status"] == "completed"
        assert len(captured) == 2 and captured[0] is captured[1]
        assert captured[0].recoveries == 1 and guard_for(rt).recoveries == 0
        assert captured[0] is not guard_for(rt)
        events = [x["event"]["type"] for x in manager.events(a["subagent_run_id"])]
        assert events.count("loop_replan") == 1 and events.count("loop_stop") == 1
        assert not rt.run_control.cancel_event.is_set()
    finally:
        manager.close()


def test_permission_change_invalidates_failure_window_and_role_does_not(tmp_path):
    rt = runtime(tmp_path)
    g = sync_guard(rt)
    for _ in range(3):
        g.observe("same", "Other", {"ok": False, "error_type": "permission_denied"})
    assert g.before_call("same")
    rt.active_agent_name = "testAgent"
    assert sync_guard(rt).before_call("same")
    rt.tool_policies["BashTool"] = "allow"
    assert sync_guard(rt).before_call("same") is None


def test_verifier_prose_cannot_reset_no_progress(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    g = sync_guard(rt := runtime(tmp_path))
    g.verification([], False)
    for _ in range(6):
        g.model_round()

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content='{"passed":false,"reason":"unique prose does not mean new facts","checks":[{"name":"unique text","passed":false,"detail":"claimed new evidence"}]}'
            )

    monkeypatch.setattr(nodes, "create_model", Model)
    # The role's one request can proceed because only one failure is known;
    # its fabricated "new check" must not reset the six rounds afterward.
    nodes.verifier_node({"runtime": rt, "task": "inspect", "messages": [], "max_attempts": 9})
    assert g.model_round().reason == "no_progress"


def test_guard_observation_is_bounded_and_accepts_cyclic_tool_data():
    g = LoopGuard()
    cycle = []
    cycle.append(cycle)
    g.observe("a" * 64, "Other", {"ok": True, "content": cycle, "exit_code": {"bad": "type"}})
    assert len(g.history) == 1 and g.history[0]["failure"]
    assert len(json.dumps(g.snapshot())) < 2000


def test_disabled_guard_preserves_old_execution_path(tmp_path):
    rt = runtime(tmp_path)
    rt.loop_guard_enabled = False
    calls = []
    tool = StructuredTool.from_function(
        lambda: calls.append(1) or {"ok": False, "error_type": "invalid_input"},
        name="FileReadTool",
        description="disabled guard fixture",
    )
    for _ in range(6):
        execute_tool(rt, tool, {})
    assert len(calls) == 6 and rt.loop_guard is None


def test_sequences_cannot_span_requirement_or_workspace_change():
    g = LoopGuard()
    for call in ['a','b']:
        g.observe(call,'Other',{'ok':True})
    g.invalidate('new-requirement')
    for call in ['a','b']*2:
        g.observe(call,'Other',{'ok':True})
    assert g.pending is None
    for call in ['a','b']:
        g.observe(call,'Other',{'ok':True})
    assert g.pending and g.pending.reason=='repeated_sequence'
    g.file_state('old-content')
    g.file_state('new-content')
    for call in ['a','b']*2:
        g.observe(call,'Other',{'ok':True})
    assert g.pending is None
