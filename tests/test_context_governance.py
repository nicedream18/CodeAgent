from __future__ import annotations

import asyncio
import json
from io import BytesIO

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool
from typer.testing import CliRunner

from mokioclaw.core.context_budget import (
    ContextBudget,
    ContextBudgetError,
    estimate_messages,
)
from mokioclaw.core.context_compaction import compact_messages, message_groups
from mokioclaw.core.context_control import ContextControl
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_output import bound_tool_result, bound_value, encoded
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tool


class SpyModel:
    def __init__(self):
        self.kwargs = {"tools": []}
        self.requests = []
        self.output = None

    def get_num_tokens_from_messages(self, messages):
        return 4 * len(messages) + sum(len(str(m.content)) // 4 for m in messages)

    def bind(self, **kwargs):
        self.output = kwargs
        return self

    def invoke(self, messages):
        self.requests.append(list(messages))
        return AIMessage(content="done")


def runtime_at(workspace, **kwargs):
    return RuntimeState(
        workspace=workspace,
        context_token_limit=6000,
        model_context_reserve_tokens=500,
        context_safety_tokens=100,
        context_control=ContextControl(),
        **kwargs,
    )


def history():
    messages = [
        SystemMessage(content="Keep all current requirements."),
        HumanMessage(content="Create report and verify it."),
    ]
    for index in range(4):
        messages += [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{index}",
                        "name": "FileReadTool",
                        "args": {"file_path": "long.txt"},
                    }
                ],
            ),
            ToolMessage(content="x" * 12000, tool_call_id=f"call-{index}"),
        ]
    return messages


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window": 100},
        {"output": 0},
        {"safety": -1},
        {"high_ratio": 0.5, "target_ratio": 0.6},
    ],
)
def test_invalid_budget_is_not_silently_reduced(kwargs):
    with pytest.raises(ContextBudgetError):
        ContextBudget(**kwargs)


def test_counter_includes_tool_schema_and_byte_fallback():
    model = SpyModel()
    messages = [HumanMessage(content="中文代码")]
    model.kwargs["tools"] = [{"name": "test", "schema": "x" * 100}]
    count, method = estimate_messages(model, messages)
    assert count > 100 and method == "model_counter+schema_bytes"
    byte_count, method = estimate_messages(None, messages)
    assert byte_count >= len("中文代码".encode()) and method == "utf8_bytes"


@pytest.mark.parametrize(
    "value",
    [
        {"ok": True, "items": ["x" * 100 for _ in range(1000)]},
        {"ok": True, **{f"field-{index}": "x" * 100 for index in range(1000)}},
        {"ok": False, "error": '中文"\n' * 100000},
        {"nested": [[[[[[[[[[["deep"]]]]]]]]]]]},
    ],
)
def test_whole_result_is_bounded_valid_json_and_idempotent(tmp_path, value):
    runtime = runtime_at(tmp_path)
    result = bound_tool_result(value, runtime, tool_name="MCP_test")
    assert len(encoded(result)) <= runtime.context_result_limit_chars
    assert json.loads(encoded(result))["output_truncated"]
    assert bound_tool_result(result, runtime) == result
    assert len(list((tmp_path / ".mokioclaw/tool-results").glob("*.json"))) == 1


def test_circular_result_and_spoofed_truncation_flag_are_bounded():
    circular = []
    circular.append(circular)
    assert bound_value({"cycle": circular})["output_truncated"]
    spoof = bound_value({"output_truncated": True, "content": "x" * 20000})
    assert len(spoof["content"]) < 12100


def test_long_exception_is_bounded_and_reported(tmp_path):
    def fail():
        raise ValueError("error " * 50000)

    tool = StructuredTool.from_function(
        name="FileReadTool", func=fail, description="failure fixture"
    )
    result = execute_tool(runtime_at(tmp_path), tool, {})
    assert result["ok"] is False and result["output_truncated"]
    assert len(encoded(result)) <= 24000


def test_long_single_line_can_be_read_at_tail_and_visibility_is_honest(tmp_path):
    (tmp_path / "long.txt").write_text(
        "x" * 200000 + "TAIL_REQUIREMENT", encoding="utf-8"
    )
    runtime = runtime_at(tmp_path)
    tool = build_tool(runtime, "FileReadTool")
    first = execute_tool(runtime, tool, {"file_path": "long.txt"})
    assert first["output_truncated"] and not first["complete"]
    assert not runtime.snapshot_for(tmp_path / "long.txt").complete
    tail = execute_tool(
        runtime,
        tool,
        {"file_path": "long.txt", "char_offset": 200000, "char_limit": 100},
    )
    assert tail["content"] == "TAIL_REQUIREMENT"


def test_bash_preserves_tail_error_and_full_log(tmp_path):
    runtime = runtime_at(tmp_path, approval_mode="auto")
    result = execute_tool(
        runtime,
        build_tool(runtime, "BashTool"),
        {"command": "python -c \"print('x' * 100000); print('TAIL_ERROR')\""},
    )
    assert "TAIL_ERROR" in result["stdout"]
    assert len(result["stdout"]) <= 6000
    assert "TAIL_ERROR" in (tmp_path / result["stdout_path"]).read_text()


def test_compaction_preserves_facts_and_uses_new_messages_without_replaying_tools(
    monkeypatch, tmp_path
):
    from mokioclaw.core import context_compaction

    monkeypatch.setattr(
        context_compaction,
        "_model_observations",
        lambda *_: {"summary": "observed files", "task": "invented task", "todos": []},
    )
    runtime = runtime_at(tmp_path)
    runtime.subagent_applied_results.add("subagent-already-applied")
    state = {
        "runtime": runtime,
        "task": "Create report",
        "acceptance_criteria": ["Keep source URL"],
        "verification_commands": ["python verify.py"],
        "latest_user_corrections": ["Use Chinese"],
        "todos": [{"id": "t1", "status": "pending", "content": "verify"}],
        "sources": [{"url": "https://example.test/source"}],
        "agent_handoffs": [
            {
                "subagent_run_id": "subagent-pending",
                "instruction": "research",
                "status": "running",
            }
        ],
    }
    messages = history()
    model, events = SpyModel(), []
    response = invoke_streaming(
        model, messages, state=state, writer=events.append, node="planner"
    )
    assert response.content == "done" and len(model.requests) == 1
    message_groups(model.requests[0])
    assert len(messages) < 10
    payload = json.loads(messages[2].content)
    facts = payload["facts"]
    assert facts["task"] == "Create report"
    assert facts["todos"][0]["status"] == "pending"
    assert facts["acceptance_criteria"] == ["Keep source URL"]
    assert facts["sources"][0]["url"] == "https://example.test/source"
    assert facts["background_tasks"][0]["subagent_run_id"] == "subagent-pending"
    assert facts["applied_subagent_results"] == ["subagent-already-applied"]
    assert model.output == {"max_tokens": 500}
    budget_event = next(event for event in events if event["type"] == "context_budget")
    assert budget_event["input_tokens"] <= budget_event["token_limit"]
    assert (tmp_path / ".mokioclaw/context/planner/current.json").exists()


@pytest.mark.parametrize("bad", [None, {"summary": 3}])
def test_invalid_summary_uses_deterministic_memory(tmp_path, bad):
    runtime = runtime_at(tmp_path)
    replacement, event = compact_messages(
        SpyModel(),
        history(),
        {"runtime": runtime, "task": "verify"},
        budget=ContextBudget.from_runtime(runtime),
        reason="manual",
        summarizer=lambda *_: bad,
    )
    assert (
        event["degraded"]
        and json.loads(replacement[2].content)["facts"]["task"] == "verify"
    )


def test_summary_transport_failure_is_bounded_and_required_overflow_is_rejected(
    tmp_path,
):
    runtime = runtime_at(tmp_path)

    def fail(*_):
        raise RuntimeError("offline")

    replacement, event = compact_messages(
        SpyModel(),
        history(),
        {"runtime": runtime, "task": "verify"},
        budget=ContextBudget.from_runtime(runtime),
        reason="manual",
        summarizer=fail,
    )
    assert event["degraded"] and len(replacement) < len(history())
    spy = SpyModel()
    with pytest.raises(ContextBudgetError, match="required"):
        invoke_streaming(
            spy,
            [SystemMessage(content="mandatory" * 50000)],
            state={"runtime": runtime},
        )
    assert not spy.requests


def test_protocol_group_must_be_complete():
    with pytest.raises(ContextBudgetError, match="incomplete|all tool"):
        message_groups(
            [
                AIMessage(
                    content="", tool_calls=[{"id": "x", "name": "tool", "args": {}}]
                )
            ]
        )
    with pytest.raises(ContextBudgetError, match="orphan"):
        message_groups([ToolMessage(content="done", tool_call_id="missing")])


def test_manual_request_applies_at_next_call_and_duplicate_requests_coalesce(
    monkeypatch, tmp_path
):
    from mokioclaw.core import context_compaction

    monkeypatch.setattr(
        context_compaction, "_model_observations", lambda *_: {"summary": "prior work"}
    )
    runtime = runtime_at(tmp_path)
    control = runtime.context_control
    first = control.request()
    assert control.request()["request_id"] == first["request_id"]
    events = []
    invoke_streaming(
        SpyModel(),
        history(),
        state={"runtime": runtime, "task": "verify"},
        node="planner",
        writer=events.append,
    )
    assert control.get(first["request_id"])["status"] == "applied"
    event = next(e for e in events if e["type"] == "context_compression")
    assert event["reason"] == "manual" and event["request_id"] == first["request_id"]
    control.close()
    assert control.request()["error_type"] == "run_finished"


def test_offline_compact_preserves_late_user_correction_and_checks_writer_lease(
    tmp_path,
):
    from mokioclaw.core.session import (
        append_assistant_turn,
        append_user_turn,
        build_session_context,
        compact_session_history,
        load_or_create_session,
        save_session,
    )
    from mokioclaw.core.session_lock import session_writer

    session = load_or_create_session(tmp_path)
    task = "intro " * 600 + "LATEST_REQUIREMENT_KEEP_SOURCE"
    turn = append_user_turn(session, task)
    for _ in range(8):
        append_assistant_turn(
            session, turn=turn, route="workflow", content="observation " * 100
        )
    save_session(tmp_path, session)
    with session_writer(tmp_path), pytest.raises(RuntimeError, match="session_busy"):
        compact_session_history(tmp_path)
    result = compact_session_history(tmp_path, keep_recent=2)
    saved = load_or_create_session(tmp_path)
    assert "LATEST_REQUIREMENT_KEEP_SOURCE" in build_session_context(tmp_path, saved)
    assert result["scope"] == "session" and result["status"] == "applied"
    assert result["after_tokens"] < result["before_tokens"]


def test_cli_run_compact_and_query(monkeypatch):
    from mokioclaw.cli.app import app
    from mokioclaw.core import daemon_client

    monkeypatch.setattr(
        daemon_client,
        "compact_run",
        lambda _: {"ok": True, "request_id": "c1", "status": "pending"},
    )
    monkeypatch.setattr(
        daemon_client,
        "compact_status",
        lambda *_: {
            "ok": True,
            "request_id": "c1",
            "status": "applied",
            "before_tokens": 900,
            "after_tokens": 100,
        },
    )
    runner = CliRunner()
    accepted = runner.invoke(app, ["compact", "--run-id", "r1"])
    assert accepted.exit_code == 0 and "pending" in accepted.output
    completed = runner.invoke(app, ["compact", "--run-id", "r1", "--request-id", "c1"])
    assert completed.exit_code == 0 and "applied" in completed.output
    assert runner.invoke(app, ["compact"]).exit_code != 0


def test_daemon_compact_rpc_uses_run_specific_control(tmp_path):
    from mokioclaw.core.daemon import AgentDaemon, RunRecord

    daemon = AgentDaemon(tmp_path / "daemon", port=0)
    record = RunRecord("r1", tmp_path / "r1")
    daemon.runs["r1"] = record

    def dispatch(method, **params):
        return daemon.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "test",
                "method": method,
                "params": {"token": daemon.token, **params},
            },
            BytesIO(),
        )["result"]

    result = dispatch("compact_run", run_id="r1")
    assert result["status"] == "pending"
    assert (
        dispatch("compact_status", run_id="r1", request_id=result["request_id"])[
            "status"
        ]
        == "pending"
    )
    assert dispatch("compact_run", run_id="missing")["error_type"] == "unknown_run"
    daemon.subagent_manager.close()
    daemon.executor.shutdown(wait=False)


def test_tui_displays_actual_compression_component_and_allows_running_shortcut(
    monkeypatch, tmp_path
):
    from mokioclaw.cli.tui.app import AgentEventMessage, MokioClawTuiApp
    from mokioclaw.core import daemon_client

    calls = []
    monkeypatch.setattr(
        daemon_client,
        "compact_run",
        lambda run: (
            calls.append(run)
            or {"ok": True, "status": "pending", "scope": "run", "request_id": "c1"}
        ),
    )

    async def scenario():
        app = MokioClawTuiApp(workspace=tmp_path)
        async with app.run_test(size=(90, 30)) as pilot:
            app.running = True
            app.active_run_id = "r1"
            await pilot.press("ctrl+k")
            await pilot.pause()
            app.post_message(
                AgentEventMessage(
                    {
                        "type": "custom_event",
                        "event": {
                            "type": "context_compression",
                            "before_tokens": 9000,
                            "after_tokens": 2000,
                            "scope": "run",
                            "status": "applied",
                        },
                    }
                )
            )
            await pilot.pause()
            # The full event is rendered in a child Static, not just in a pure formatter.
            content = "\n".join(str(widget.render()) for widget in app.query("Static"))
            assert "9000" in content and "2000" in content
            assert calls == ["r1"]

    asyncio.run(scenario())


def test_trace_metrics_are_deduplicated(tmp_path):
    from mokioclaw.core.trace import TraceRecorder

    trace = TraceRecorder(runtime_at(tmp_path))
    trace.start({"task": "test"})
    event = {"type": "context_compression", "event_id": "same", "status": "applied"}
    trace.record_custom_event(event)
    trace.record_custom_event(event)
    trace.record_custom_event(
        {
            "type": "context_budget",
            "input_tokens": 500,
            "estimation_method": "utf8_bytes",
        }
    )
    metrics = trace.summary_payload()["context_governance"]
    assert metrics["compactions"] == 1 and metrics["max_input_tokens"] == 500


def test_repeated_compaction_checkpoint_restores_sources_and_applied_results(tmp_path):
    from mokioclaw.core.checkpoint import CheckpointManager, load_resume_inputs

    runtime = runtime_at(tmp_path, checkpoint_mode="strict")
    state = {
        "runtime": runtime,
        "task": "finish report",
        "messages": history(),
        "acceptance_criteria": ["preserve all sources"],
        "sources": [{"url": f"https://example.test/{i}"} for i in range(100)],
    }
    runtime.subagent_applied_results.add("subagent-done")
    for index in range(2):
        replacement, _event = compact_messages(
            SpyModel(),
            state["messages"],
            state,
            budget=ContextBudget.from_runtime(runtime),
            reason="manual",
            node="planner",
            summarizer=lambda *_: {"summary": "prior discoveries"},
        )
        state["messages"] = replacement
        if index == 0:
            state["messages"] += history()[2:]
    CheckpointManager(runtime).save(state, status="running", latest_node="planner")
    restored_runtime = runtime_at(tmp_path, checkpoint_mode="strict")
    restored, _ = load_resume_inputs(restored_runtime, task=None, max_attempts=3)
    assert restored_runtime.context_version == 2
    assert "subagent-done" in restored_runtime.subagent_applied_results
    assert len(restored["sources"]) == 100
    assert restored["acceptance_criteria"] == ["preserve all sources"]
    light_runtime = runtime_at(tmp_path, checkpoint_mode="light")
    light, _ = load_resume_inputs(light_runtime, task=None, max_attempts=3)
    assert len(light["sources"]) == 100
    assert "subagent-done" in light_runtime.subagent_applied_results


def test_memory_source_catalogue_is_bounded_without_losing_full_sources(tmp_path):
    from mokioclaw.graph.memory import build_layered_memory

    sources = [
        {"url": "https://example.test/" + str(i), "title": "title" * 50}
        for i in range(1000)
    ]
    memory = build_layered_memory(
        {"runtime": runtime_at(tmp_path), "task": "research", "sources": sources}
    )
    working = memory["working_memory"]
    assert len(encoded(working["sources"])) < 4000
    catalogue = json.loads((tmp_path / working["source_catalogue"]).read_text())
    assert len(catalogue) == 1000


def test_summary_itself_uses_guarded_bounded_input(monkeypatch, tmp_path):
    from mokioclaw.providers import openai_provider

    class SummarySpy(SpyModel):
        def invoke(self, messages):
            self.requests.append(list(messages))
            return AIMessage(content='{"summary":"kept observations"}')

    model = SummarySpy()
    monkeypatch.setattr(openai_provider, "create_model", lambda: model)
    state = {"runtime": runtime_at(tmp_path), "task": "verify"}
    compact_messages(
        SpyModel(),
        history(),
        state,
        budget=ContextBudget.from_runtime(state["runtime"]),
        reason="manual",
    )
    assert len(model.requests) == 1
    count, _ = estimate_messages(model, model.requests[0])
    assert count <= ContextBudget.from_runtime(state["runtime"]).target


def test_code_agent_completes_artifact_after_multiple_local_compactions(
    monkeypatch, tmp_path
):
    from mokioclaw.agents import code_agent
    from mokioclaw.core import context_compaction

    text = ("filler " * 100 + "\n") * 1000 + "TAIL_CRITERION: PASS_WITH_SOURCE"
    (tmp_path / "spec.txt").write_text(text)

    class ArtifactModel(SpyModel):
        calls = 0

        def bind_tools(self, tools):
            from langchain_core.utils.function_calling import convert_to_openai_tool

            self.kwargs["tools"] = [convert_to_openai_tool(tool) for tool in tools]
            return self

        def invoke(self, messages):
            self.requests.append(list(messages))
            self.calls += 1
            if self.calls <= 5:
                args = {"file_path": "spec.txt", "offset": self.calls * 10, "limit": 50}
                name = "FileReadTool"
            elif self.calls == 6:
                args = {
                    "file_path": "spec.txt",
                    "char_offset": len(text) - 100,
                    "char_limit": 100,
                }
                name = "FileReadTool"
            elif self.calls == 7:
                tail = json.loads(messages[-1].content)["content"].split(
                    "TAIL_CRITERION: "
                )[-1]
                args = {
                    "file_path": "report.json",
                    "content": json.dumps(
                        {"criterion": tail, "source": "https://example.test/source"}
                    ),
                }
                name = "FileWriteTool"
            else:
                return AIMessage(content="report written")
            return AIMessage(
                content="",
                tool_calls=[{"id": f"step-{self.calls}", "name": name, "args": args}],
            )

    model = ArtifactModel()
    monkeypatch.setattr(code_agent, "create_model", lambda: model)
    monkeypatch.setattr(
        context_compaction,
        "_model_observations",
        lambda *_: {"summary": "read specification; report remains pending"},
    )
    runtime = runtime_at(tmp_path)
    runtime.active_agent_tools = {"FileReadTool", "FileWriteTool"}
    state = {
        "runtime": runtime,
        "task": "Read spec tail and write report with the given source",
        "todos": [],
        "sources": [{"url": "https://example.test/source"}],
        "acceptance_criteria": ["report criterion equals spec tail"],
    }
    events = []
    result = code_agent.run_code_agent(
        state, "Finish report.json", writer=events.append
    )
    report = json.loads((tmp_path / "report.json").read_text())
    assert report == {
        "criterion": "PASS_WITH_SOURCE",
        "source": "https://example.test/source",
    }
    assert result["ok"]
    assert sum(e["type"] == "context_compression" for e in events) >= 2
    assert (
        sum(e["type"] == "tool_call" and e["name"] == "FileWriteTool" for e in events)
        == 1
    )
    assert all(
        e["input_tokens"] <= e["token_limit"]
        for e in events
        if e["type"] == "context_budget"
    )
    for request in model.requests:
        message_groups(request)


@pytest.mark.parametrize("parameter", ["max_tokens", "max_completion_tokens"])
def test_transport_honors_the_selected_output_parameter(monkeypatch, parameter):
    from mokioclaw.providers.openai_provider import create_model

    monkeypatch.setenv("API_KEY", "test-only-not-a-real-key")
    monkeypatch.setenv("MODEL", "custom-model")
    monkeypatch.setenv("BASE_URL", "http://localhost:9/v1")
    monkeypatch.setenv("MOKIO_MODEL_OUTPUT_PARAMETER", parameter)
    model = create_model()
    payload = model._get_request_payload([HumanMessage(content="test")])
    assert payload[parameter] == 8192
    assert ("max_tokens" in payload) != ("max_completion_tokens" in payload)


def test_background_results_keep_independent_budgets_and_merge_sources_once(
    monkeypatch, tmp_path
):
    import copy
    import threading
    import time

    from mokioclaw.agents.registry import registered_subagents
    from mokioclaw.core import context_compaction
    from mokioclaw.core.subagent_tasks import SubagentTaskManager
    from mokioclaw.graph import nodes

    manager = SubagentTaskManager(tmp_path / "runs", max_running=2)
    runtime = runtime_at(tmp_path)
    runtime.parent_run_id = "parent"
    runtime.subagent_task_manager = manager
    runtime.subagent_registry = registered_subagents()
    state = {
        "runtime": runtime,
        "task": "research",
        "sources": [],
        "research_notes": "",
        "agent_handoffs": [],
    }
    barrier = threading.Barrier(2)
    controls = []
    monkeypatch.setattr(
        context_compaction,
        "_model_observations",
        lambda *_: {"summary": "research observations"},
    )

    def research(child_state, instruction, *, writer, **_):
        controls.append(child_state["runtime"].context_control)
        barrier.wait(timeout=3)
        invoke_streaming(
            SpyModel(), history(), state=child_state, writer=writer, node="searchAgent"
        )
        child_state["sources"] = [{"url": "https://example.test/" + instruction}]
        child_state["research_notes"] = instruction
        return {"ok": True, "summary": instruction}

    monkeypatch.setattr(nodes, "run_search_agent", research)
    request = runtime.context_control.request()
    try:
        accepted = [
            nodes._call_registered_subagent_tool(
                state,
                lambda _: None,
                "searchAgent",
                instruction,
                run_in_background=True,
            )
            for instruction in ("a", "b")
        ]
        for item in accepted:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                task = manager.get(item["subagent_run_id"])
                if task["status"] in {"completed", "failed"}:
                    break
                time.sleep(0.01)
            assert task["status"] == "completed", task
            nodes._agent_result_tool(state, lambda _: None, item["subagent_run_id"])
        assert len(state["sources"]) == 2
        before = copy.deepcopy(state["sources"])
        nodes._agent_result_tool(state, lambda _: None, accepted[0]["subagent_run_id"])
        assert state["sources"] == before
        assert len({id(control) for control in controls}) == 2
        assert all(control is not runtime.context_control for control in controls)
        assert runtime.context_control.get(request["request_id"])["status"] == "pending"
        invoke_streaming(SpyModel(), history(), state=state, node="planner")
        assert runtime.context_control.get(request["request_id"])["status"] == "applied"
    finally:
        manager.close()


@pytest.mark.parametrize(
    "count,attempted,blocked",
    [(594, False, False), (595, True, False), (700, True, False), (701, True, True)],
)
def test_request_high_and_hard_boundaries(
    monkeypatch, tmp_path, count, attempted, blocked
):
    from mokioclaw.core import context_compaction

    class FixedCounter(SpyModel):
        def get_num_tokens_from_messages(self, messages):
            return count

    calls = []

    def compact(model, messages, state, **kwargs):
        calls.append(True)
        return messages, {"status": "noop"}

    monkeypatch.setattr(context_compaction, "compact_messages", compact)
    runtime = RuntimeState(
        workspace=tmp_path,
        context_token_limit=1000,
        model_context_reserve_tokens=200,
        context_safety_tokens=100,
    )
    model = FixedCounter()
    if blocked:
        with pytest.raises(ContextBudgetError):
            invoke_streaming(
                model, [HumanMessage(content="task")], state={"runtime": runtime}
            )
        assert not model.requests
    else:
        invoke_streaming(
            model, [HumanMessage(content="task")], state={"runtime": runtime}
        )
        assert model.requests
    assert bool(calls) == attempted


def test_real_stdio_mcp_large_result_is_bounded(tmp_path):
    import sys
    from pathlib import Path

    from mokioclaw.tools.mcp_tools import build_mcp_tools

    runtime = runtime_at(tmp_path, approval_mode="auto")
    runtime.mcp_servers = [
        {
            "name": "large",
            "command": sys.executable,
            "args": [
                str(Path(__file__).resolve().parents[1] / "examples/mcp/echo_server.py")
            ],
        }
    ]
    tools = build_mcp_tools(runtime)
    tool = next(tool for tool in tools if tool.name == "MCP_large_echo")
    events = []
    result = execute_tool(
        runtime, tool, {"text": "中文" * 100000}, on_event=events.append
    )
    assert result["output_truncated"] and len(encoded(result)) <= 24000
    assert (tmp_path / result["full_output_path"]).is_file()
    assert any(event["type"] == "tool_output_truncated" for event in events)


def test_first_registered_code_agent_handles_unset_summary_fields(
    monkeypatch, tmp_path
):
    from mokioclaw.agents import code_agent
    from mokioclaw.agents.registry import run_subagent

    class InitialModel(SpyModel):
        def bind_tools(self, tools):
            return self

    monkeypatch.setattr(code_agent, "create_model", lambda: InitialModel())
    result = run_subagent(
        {"runtime": runtime_at(tmp_path), "task": "create report", "todos": []},
        "codeAgent",
        "implement",
    )
    assert result["ok"] and result["summary"] == "done"


def test_session_compact_archives_all_checkpoint_sources(tmp_path):
    from mokioclaw.core.checkpoint import CheckpointManager
    from mokioclaw.core.session import (
        append_assistant_turn,
        append_user_turn,
        compact_session_history,
        load_or_create_session,
        save_session,
    )

    runtime = runtime_at(tmp_path)
    sources = [{"url": f"https://example.test/{i}"} for i in range(100)]
    CheckpointManager(runtime).save(
        {"runtime": runtime, "task": "report", "sources": sources}, status="finished"
    )
    session = load_or_create_session(tmp_path)
    turn = append_user_turn(session, "keep source evidence")
    for _ in range(5):
        append_assistant_turn(session, turn=turn, route="workflow", content="notes")
    save_session(tmp_path, session)
    result = compact_session_history(tmp_path, keep_recent=2)
    facts = json.loads((tmp_path / result["full_facts_path"]).read_text())
    assert len(facts["sources"]) == 100


def test_compaction_does_not_retime_old_tool_group_after_latest_correction(tmp_path):
    messages = history() + [
        HumanMessage(content="LATEST: keep the source and do not repeat writes")
    ]
    runtime = runtime_at(tmp_path)
    replacement, event = compact_messages(
        SpyModel(),
        messages,
        {"runtime": runtime, "task": "finish"},
        budget=ContextBudget.from_runtime(runtime),
        reason="manual",
        summarizer=lambda *_: {"summary": "historical reads"},
    )
    assert any(
        isinstance(item, HumanMessage) and "LATEST" in item.content
        for item in replacement
    )
    assert not any(isinstance(item, ToolMessage) for item in replacement)
    assert event["removed_messages"] == 8


def test_code_agent_updates_authoritative_todo_before_next_call(monkeypatch, tmp_path):
    from mokioclaw.agents import code_agent
    from mokioclaw.core.context_compaction import critical_facts

    runtime = runtime_at(tmp_path)
    state = {
        "runtime": runtime,
        "task": "verify",
        "todos": [{"id": "t1", "content": "verify", "status": "pending", "note": ""}],
    }

    class TodoModel(SpyModel):
        count = 0

        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            self.count += 1
            if self.count == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "todo",
                            "name": "TodoUpdateTool",
                            "args": {"todo_id": "t1", "status": "completed"},
                        }
                    ],
                )
            assert critical_facts(state)["todos"][0]["status"] == "completed"
            return AIMessage(content="verified")

    monkeypatch.setattr(code_agent, "create_model", lambda: TodoModel())
    result = code_agent.run_code_agent(state, "finish verification")
    assert result["todos"][0]["status"] == "completed"


def test_repeated_identical_write_after_compaction_has_no_second_side_effect(tmp_path):
    runtime = runtime_at(tmp_path)
    tool = build_tool(runtime, "FileWriteTool")
    arguments = {"file_path": "report.json", "content": '{"verified": true}'}
    first = execute_tool(runtime, tool, arguments)
    mtime = (tmp_path / "report.json").stat().st_mtime_ns
    compact_messages(
        SpyModel(),
        history(),
        {"runtime": runtime, "task": "verify report"},
        budget=ContextBudget.from_runtime(runtime),
        reason="manual",
        summarizer=lambda *_: {"summary": "report written"},
    )
    second = execute_tool(runtime, tool, arguments)
    assert first["type"] == "create"
    assert second["already_applied"] and second["type"] == "unchanged"
    assert (tmp_path / "report.json").stat().st_mtime_ns == mtime


def test_pending_compact_can_be_cancelled_but_in_progress_is_not_interrupted():
    control = ContextControl()
    first = control.request()
    assert control.cancel(first["request_id"])["status"] == "cancelled"
    assert control.take() is None
    second = control.request()
    assert control.take()["request_id"] == second["request_id"]
    assert (
        control.cancel(second["request_id"])["error_type"] == "compaction_in_progress"
    )
    assert control.get(second["request_id"])["status"] == "applying"
    assert control.close()[0]["status"] == "failed"
    assert control.cancel("missing")["error_type"] == "unknown_compact_request"


def test_cli_cancels_request_and_rejects_incomplete_cancel(monkeypatch):
    from mokioclaw.cli.app import app
    from mokioclaw.core import daemon_client

    monkeypatch.setattr(
        daemon_client, "compact_cancel", lambda *_: {"ok": True, "status": "cancelled"}
    )
    runner = CliRunner()
    result = runner.invoke(
        app, ["compact", "--run-id", "r1", "--request-id", "c1", "--cancel"]
    )
    assert result.exit_code == 0 and "cancelled" in result.output
    assert runner.invoke(app, ["compact", "--run-id", "r1", "--cancel"]).exit_code != 0


def test_live_daemon_rpc_applies_manual_compact_and_persists_completion(
    monkeypatch, tmp_path
):
    import threading

    from mokioclaw.core import agent, context_compaction, daemon_client
    from mokioclaw.core.context_control import CURRENT_CONTROL
    from mokioclaw.core.daemon import AgentDaemon

    daemon_dir = tmp_path / "daemon"
    monkeypatch.setenv("MOKIO_DAEMON_DIR", str(daemon_dir))
    monkeypatch.setenv("MOKIO_DAEMON_WORKER", "")
    monkeypatch.setattr(
        context_compaction, "_model_observations", lambda *_: {"summary": "prior work"}
    )
    daemon = AgentDaemon(daemon_dir, port=0)
    server = threading.Thread(target=daemon.run, daemon=True)
    ready, release = threading.Event(), threading.Event()
    received, errors = [], []

    def fake_stream(task, **kwargs):
        runtime = runtime_at(tmp_path / "workspace")
        runtime.context_control = CURRENT_CONTROL.get()
        ready.set()
        yield {"type": "workspace", "path": str(runtime.workspace)}
        assert release.wait(5)
        events = []
        invoke_streaming(
            SpyModel(),
            history(),
            state={"runtime": runtime, "task": task},
            writer=events.append,
            node="planner",
        )
        for event in events:
            yield {"type": "custom_event", "event": event}

    monkeypatch.setattr(agent, "stream_agent_events", fake_stream)

    def consume():
        try:
            received.extend(
                daemon_client.run_via_daemon(
                    "run", "verify report", options={"approval_mode": "auto"}
                )
            )
        except Exception as exc:  # noqa: BLE001 - forward thread failures to the test
            errors.append(exc)

    consumer = threading.Thread(target=consume, daemon=True)
    server.start()
    try:
        # ensure_daemon discovers this server; it must not start another one.
        import time

        deadline = time.monotonic() + 3
        while (
            not (daemon_dir / "address.json").exists() and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (daemon_dir / "address.json").exists()
        consumer.start()
        assert ready.wait(3)
        run_id = next(iter(daemon.runs))
        request = daemon_client.compact_run(run_id)
        assert request["status"] == "pending"
        release.set()
        consumer.join(timeout=5)
        assert not consumer.is_alive() and not errors
        completed = daemon_client.compact_status(run_id, request["request_id"])
        assert completed["status"] == "applied"
        assert received[0]["run_id"] == run_id
        assert any(
            event.get("event", {}).get("type") == "context_compression"
            for event in received
        )
        persisted = (daemon_dir / "runs" / run_id / "events.jsonl").read_text()
        assert request["request_id"] in persisted and '"status": "applied"' in persisted
    finally:
        release.set()
        if daemon.server is not None:
            daemon_client.stop_daemon()
        consumer.join(timeout=3) if consumer.ident else None
        server.join(timeout=3)
