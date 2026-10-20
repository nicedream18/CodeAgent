from __future__ import annotations

import asyncio
import json
import multiprocessing
import time
from io import BytesIO
from pathlib import Path

import pytest
import test_mcp_streaming as mcp_examples
from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core import hooks
from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.checkpoint import CheckpointManager, load_resume_inputs
from mokioclaw.core.daemon import AgentDaemon
from mokioclaw.core.hooks import HookDecision, register_hook
from mokioclaw.core.instructions import inject_instructions, instructions
from mokioclaw.core.state import RuntimeState
from mokioclaw.orchestration.runtime import single_step_workflow
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.file_tools import read_file, write_file
from mokioclaw.tools.mcp_tools import search_mcp_tools
from mokioclaw.tools.registry import build_tools
from mokioclaw.tools.skills import discover_skills, load_skill

mcp_runtime = mcp_examples.mcp_runtime


def callback_warn(context):
    print("x" * 20000)
    return HookDecision("warn", context.tool_call_id)


def callback_sleep(context):
    time.sleep(10)
    return HookDecision("warn", "late")


def callback_invalid(context):
    return {"action": "allow", "args": {"file_path": "bypass"}}


@pytest.fixture(autouse=True)
def isolate_hooks(monkeypatch):
    monkeypatch.setattr(hooks, "_CALLBACKS", {})


def request(daemon, method, **params):
    return daemon.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "test",
            "method": method,
            "params": {"token": daemon.token, **params},
        },
        BytesIO(),
    )["result"]


def graph(node):
    return single_step_workflow("step", node)


def test_stdio_discovery_deferred_until_search_and_dynamically_bound(mcp_runtime, monkeypatch):
    from mokioclaw.agents import code_agent

    runtime = mcp_runtime
    pid_log = Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"])

    class Model:
        def __init__(self):
            self.names, self.calls = [], 0

        def bind_tools(self, tools):
            self.names = [t.name for t in tools]
            return self

        def invoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                assert not any(name.startswith("MCP_") for name in self.names)
                assert not pid_log.exists()
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "ToolSearchTool",
                            "args": {"server": "test"},
                            "id": "search",
                        }
                    ],
                )
            if self.calls == 2:
                assert "MCP_test_echo" in self.names
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "MCP_test_echo",
                            "args": {"text": "hello"},
                            "id": "echo",
                        }
                    ],
                )
            return AIMessage(content="MCP 完成")

    model, events = Model(), []
    monkeypatch.setattr(code_agent, "create_model", lambda: model)
    result = code_agent.run_code_agent(
        {"runtime": runtime, "task": "echo"}, "echo", writer=events.append
    )
    assert result["summary"] == "MCP 完成"
    assert len(pid_log.read_text().splitlines()) == 2  # discovery + one call
    assert any(
        e["type"] == "tool_result" and e["name"] == "MCP_test_echo" and e["result"]["ok"]
        for e in events
    )
    search_mcp_tools(runtime, server="test")
    assert len(pid_log.read_text().splitlines()) == 2
    search_mcp_tools(runtime, server="test", refresh=True)
    assert len(pid_log.read_text().splitlines()) == 3


def test_skill_listing_does_not_start_mcp_but_load_validates_reference(mcp_runtime):
    runtime = mcp_runtime
    root = runtime.workspace / ".mokioclaw/skills/echo"
    root.mkdir(parents=True)
    (root / "SKILL.md").write_text(
        "---\nname: echo\ndescription: local echo\ntools: [MCP_test_echo]\npermissions: [mcp]\n---\nCall echo.\n"
    )
    pid_log = Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"])
    assert discover_skills(runtime)[0]["valid"]
    assert not pid_log.exists()
    assert load_skill(runtime, "echo")["ok"]
    assert "MCP_test_echo" in runtime.mcp_loaded_tools
    assert len(pid_log.read_text().splitlines()) == 1


def test_tool_search_unknown_service_and_permissions_do_not_expand_scope(mcp_runtime):
    runtime = mcp_runtime
    assert search_mcp_tools(runtime, query="unrelated")["tools"] == []
    assert not Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"]).exists()
    runtime.active_agent_tools = {"FileReadTool"}
    search = next(t for t in build_tools(runtime) if t.name == "ToolSearchTool")
    assert not execute_tool(runtime, search, {"server": "test"})["ok"]
    assert not runtime.mcp_loaded_tools


def test_hook_order_block_and_post_context(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies["FileWriteTool"] = "ask"
    approvals = []
    runtime.approval_handler = lambda request: approvals.append(request) or ApprovalDecision(True)
    runtime.settings = {
        "hooks": {
            "rules": [
                {
                    "event": "PreToolUse",
                    "tool": "FileWriteTool",
                    "args": {"file_path": "^generated/"},
                    "action": "block",
                    "message": "generated files are immutable",
                },
                {
                    "event": "PostToolUse",
                    "tool": "FileWriteTool",
                    "action": "add_context",
                    "message": "Verify the changed file",
                },
            ]
        }
    }
    tool = next(t for t in build_tools(runtime) if t.name == "FileWriteTool")
    denied = execute_tool(runtime, tool, {"file_path": "generated/a.txt", "content": "x"})
    assert not denied["ok"] and not approvals and not (tmp_path / "generated").exists()
    result = execute_tool(runtime, tool, {"file_path": "a.txt", "content": "x"})
    assert result["ok"] and len(approvals) == 1
    assert runtime.hook_context == ["Verify the changed file"]


def test_hooks_once_per_logical_call_across_retry_and_static_deny_first(tmp_path):
    runtime = RuntimeState(workspace=tmp_path)
    runtime.settings = {
        "hooks": {
            "rules": [
                {
                    "event": name,
                    "tool": "FileReadTool",
                    "action": "warn",
                    "message": name,
                }
                for name in ("PreToolUse", "PostToolUse")
            ]
        }
    }
    attempts = []

    def flaky(file_path):
        attempts.append(file_path)
        return {"ok": len(attempts) > 1, "error_type": "transient"}

    tool = StructuredTool.from_function(name="FileReadTool", func=flaky, description="read")
    events = []
    assert execute_tool(runtime, tool, {"file_path": "x"}, on_event=events.append)["ok"]
    assert len(attempts) == 2
    assert len([e for e in events if e["type"] == "hook_result"]) == 2
    events.clear()
    runtime.tool_policies["FileReadTool"] = "deny"
    execute_tool(runtime, tool, {"file_path": "x"}, on_event=events.append)
    assert not any(e["type"] == "hook_result" for e in events)


@pytest.mark.parametrize("callback", [callback_warn, callback_sleep, callback_invalid])
def test_python_hooks_bounded_timeout_and_no_worker_leaks(tmp_path, callback):
    runtime = RuntimeState(workspace=tmp_path)
    runtime.settings = {"hooks": {"timeout_seconds": 0.1 if callback is callback_sleep else 5}}
    register_hook("PreToolUse", callback)
    events, invoked = [], []
    tool = StructuredTool.from_function(
        name="FileReadTool",
        func=lambda file_path: invoked.append(file_path) or {"ok": True},
        description="read",
    )
    before = {p.pid for p in multiprocessing.active_children()}
    result = execute_tool(
        runtime,
        tool,
        {"file_path": "x"},
        tool_call_id="hook-call",
        on_event=events.append,
    )
    assert result["ok"] is (callback is callback_warn)
    assert bool(invoked) is (callback is callback_warn)
    assert {p.pid for p in multiprocessing.active_children()} == before
    assert all(len(e.get("logs", "")) <= 4096 for e in events)


def test_actual_daemon_approval_correction_replans_and_rejects_late_approval(tmp_path, monkeypatch):
    from mokioclaw.core import agent

    workspace = tmp_path / "workspace"
    monkeypatch.setenv("MOKIO_TOOL_POLICIES", '{"FileWriteTool":"ask"}')
    monkeypatch.setattr(
        agent,
        "build_entry_workflow",
        lambda: graph(lambda s: {"intent_route": "workflow"}),
    )

    def step(state):
        if state.get("latest_user_corrections"):
            return {"final_answer": "已按补充要求停止修改", "passed": True}
        tool = next(t for t in build_tools(state["runtime"]) if t.name == "FileWriteTool")
        execute_tool(
            state["runtime"],
            tool,
            {"file_path": "database.py", "content": "old plan"},
            tool_call_id="old-write",
        )
        return {"final_answer": "old"}

    monkeypatch.setattr(agent, "build_complex_workflow", lambda: graph(step))
    daemon = AgentDaemon(tmp_path / "daemon")
    try:
        run_id = daemon.start_run(
            {
                "options": {
                    "task": "edit database",
                    "workspace": str(workspace),
                    "checkpoint_mode": "strict",
                }
            }
        )
        record = daemon.runs[run_id]
        deadline = time.monotonic() + 5
        while not record.waiters and record.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert record.waiters, record.events
        approval_id = next(iter(record.waiters))
        accepted = request(
            daemon,
            "submit_run_message",
            run_id=run_id,
            message_id="correction",
            text="不要修改数据库",
        )
        assert accepted["ok"] and accepted["status"] == "queued"
        assert not request(
            daemon,
            "resolve_approval",
            run_id=run_id,
            approval_id=approval_id,
            tool_call_id="old-write",
            approved=True,
        )["ok"]
        while record.status == "running" and time.monotonic() < deadline:
            time.sleep(0.01)
        assert record.status == "finished", record.events
        assert not (workspace / "database.py").exists()
        assert (
            request(
                daemon,
                "submit_run_message",
                run_id=run_id,
                message_id="correction",
                text="不要修改数据库",
            )["status"]
            == "applied"
        )
        saved = json.loads((workspace / ".mokioclaw/checkpoints/state.json").read_text())
        assert len(saved["latest_user_corrections"]) == 1
    finally:
        for record in daemon.runs.values():
            record.run_control.cancel()
        daemon.executor.shutdown(wait=True)


@pytest.mark.parametrize("mode", ["light", "strict"])
def test_checkpoint_restores_nested_rules_and_requires_reverification_after_rewind(tmp_path, mode):
    from mokioclaw.core.changes import rewind

    (tmp_path / "src").mkdir()
    (tmp_path / "src/AGENTS.md").write_text("nested rule")
    (tmp_path / "src/a.txt").write_text("before")
    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode=mode)
    read_file(runtime, "src/a.txt")
    inject_instructions(runtime, [])
    assert write_file(runtime, "src/a.txt", "after")["ok"]
    CheckpointManager(runtime).save(
        {"runtime": runtime, "task": "edit", "passed": True}, status="finished"
    )
    assert rewind(tmp_path, runtime.run_id, apply=True)["ok"]
    restored_runtime = RuntimeState(workspace=tmp_path, checkpoint_mode=mode)
    restored, _ = load_resume_inputs(restored_runtime)
    assert "nested rule" in instructions(restored_runtime).text()
    # The public restore API must invalidate strict and light verification.
    assert not restored.get("passed")


def test_custom_stream_event_does_not_clear_inbox_while_producer_still_runs(tmp_path):
    from langchain_core.messages import AIMessageChunk, HumanMessage

    from mokioclaw.core.agent import _graph_envelopes
    from mokioclaw.core.run_control import RunControl
    from mokioclaw.orchestration.runtime import get_stream_writer
    from mokioclaw.providers.openai_provider import invoke_streaming

    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)
    sent = False

    class SlowModel:
        def __init__(self, corrected):
            self.corrected = corrected

        async def astream(self, messages):
            await asyncio.sleep(0.05)
            if self.corrected:
                yield AIMessageChunk(content="updated")
            else:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "FileWriteTool",
                            "args": '{"file_path":"stale.txt","content":"stale"}',
                            "id": "old",
                            "index": 0,
                        }
                    ],
                )

    def node(state):
        nonlocal sent
        raw = get_stream_writer()

        def writer(event):
            nonlocal sent
            if event["type"] == "llm_stream_start" and not sent:
                sent = True
                control.inbox.submit("correction", "do not write")
            raw(event)

        response = invoke_streaming(
            SlowModel(bool(state.get("latest_user_corrections"))),
            [HumanMessage(content="task")],
            state=state,
            writer=writer,
        )
        for call in response.tool_calls:
            write_file(runtime, **call["args"])
        return {"final_answer": response.content}

    state = {"runtime": runtime, "task": "task"}
    try:
        events = list(_graph_envelopes(graph(node), state, terminal=True))
        assert state["final_answer"] == "updated"
        assert not (tmp_path / "stale.txt").exists()
        ends = [e["event"] for e in events if e.get("event", {}).get("type") == "llm_stream_end"]
        assert len(ends) == 2 and len({e["stream_id"] for e in ends}) == 2
        assert all(e["status"] == "finished" for e in ends)
    finally:
        control.close()


def test_sync_subagent_correction_keeps_completed_write_and_closes_old_tool_group(
    tmp_path, monkeypatch
):
    from mokioclaw.agents import code_agent
    from mokioclaw.agents.registry import run_subagent
    from mokioclaw.core.agent import _graph_envelopes
    from mokioclaw.core.run_control import RunControl
    from mokioclaw.orchestration.runtime import get_stream_writer

    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileWriteTool",
                        "id": "completed",
                        "args": {"file_path": "kept.txt", "content": "kept"},
                    },
                    {
                        "name": "FileWriteTool",
                        "id": "obsolete",
                        "args": {"file_path": "database.py", "content": "obsolete"},
                    },
                ],
            )

    monkeypatch.setattr(code_agent, "create_model", Model)

    def node(state):
        if state.get("latest_user_corrections"):
            return {"final_answer": "replanned", "passed": True}
        runtime.foreground_state = state
        raw = get_stream_writer()

        def writer(event):
            raw(event)
            if event["type"] == "tool_result" and event.get("tool_call_id") == "completed":
                control.inbox.submit("new", "不要修改数据库")

        return run_subagent(state, "codeAgent", "create both files", writer=writer).get(
            "state_updates", {}
        )

    state = {"runtime": runtime, "task": "create files"}
    try:
        events = list(_graph_envelopes(graph(node), state, terminal=True))
    finally:
        control.close()
    assert (tmp_path / "kept.txt").read_text() == "kept"
    assert not (tmp_path / "database.py").exists()
    result = next(
        row["event"]["result"]
        for row in events
        if row.get("event", {}).get("tool_call_id") == "obsolete"
        and row["event"]["type"] == "tool_result"
    )
    assert result["status"] == "not_executed"
    assert state["final_answer"] == "replanned" and len(state["latest_user_corrections"]) == 1


def test_mcp_configuration_change_invalidates_only_runtime_catalog(mcp_runtime):
    runtime = mcp_runtime
    pid_log = Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"])
    search_mcp_tools(runtime, server="test")
    assert len(pid_log.read_text().splitlines()) == 1
    runtime.mcp_servers[0]["description"] = "updated local service purpose"
    build_tools(runtime)
    assert not runtime.mcp_loaded_tools
    assert len(pid_log.read_text().splitlines()) == 1  # invalidation does not discover eagerly
    search_mcp_tools(runtime, server="test")
    assert len(pid_log.read_text().splitlines()) == 2
