from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessageChunk
from langchain_core.tools import StructuredTool

from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.session import (
    compact_session_history,
    load_or_create_session,
    save_session,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_policy import authorize_tool, parse_tool_policies
from mokioclaw.agents.registry import parse_subagent_configs
from mokioclaw.agents.registry import SubagentDefinition, SubagentRegistry
from mokioclaw.graph.nodes import _call_registered_subagent_tool
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools.execution import ToolExecutionError, execute_tool
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.tools.mcp_tools import build_mcp_tools
from mokioclaw.tools.skills import discover_skills, load_skill, parse_skill
from mokioclaw.graph.nodes import context_monitor_node


def test_parse_tool_policies_and_single_operation_approval(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies = parse_tool_policies(
        '{"FileWriteTool":"ask","BashTool":"deny"}'
    )
    requests = []
    runtime.approval_handler = lambda request: (
        requests.append(request) or ApprovalDecision(True)
    )

    assert authorize_tool(runtime, "FileWriteTool", {"file_path": "index.html"}) is None
    assert requests[0].tool_name == "FileWriteTool"
    assert (
        authorize_tool(runtime, "BashTool", {"command": "echo safe"})["error_type"]
        == "permission_denied"
    )


def test_mcp_tools_require_an_explicit_single_operation_approval(
    tmp_path: Path,
) -> None:
    requests = []
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.approval_handler = lambda request: (
        requests.append(request) or ApprovalDecision(True)
    )
    assert authorize_tool(runtime, "MCP_docs_lookup", {"query": "x"}) is None
    assert requests[0].tool_name == "MCP_docs_lookup"


def test_context_monitor_reserves_model_budget(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MOKIO_CONTEXT_TOKEN_LIMIT", "1000")
    monkeypatch.setattr(
        "mokioclaw.graph.nodes.estimate_context_tokens", lambda state: 850
    )
    result = context_monitor_node(
        {"runtime": RuntimeState(workspace=tmp_path), "task": "demo"}
    )
    assert result["context_should_compress"] is True
    assert result["context_token_limit"] == 800


def test_invalid_tool_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="JSON object"):
        parse_tool_policies("[]")
    with pytest.raises(ValueError, match="invalid policy"):
        parse_tool_policies('{"BashTool":"maybe"}')


def test_subagent_config_enforces_budgets_and_tool_lists() -> None:
    config = parse_subagent_configs(
        '{"searchAgent":{"max_tool_calls":3,"allowed_tools":["WebSearchTool"]}}'
    )
    assert config["searchAgent"]["max_tool_calls"] == 3
    assert config["searchAgent"]["allowed_tools"] == ["WebSearchTool"]
    with pytest.raises(ValueError, match="between 1 and 50"):
        parse_subagent_configs('{"searchAgent":{"max_tool_calls":0}}')


def test_custom_subagent_registers_and_returns_handoff_contract(tmp_path: Path) -> None:
    def run_auditor(state, instruction, *, writer=None, max_loops=4):
        return {
            "ok": True,
            "summary": f"checked {instruction}",
            "evidence": ["report.txt"],
            "unresolved": [],
        }

    runtime = RuntimeState(workspace=tmp_path)
    runtime.subagent_registry = SubagentRegistry()
    runtime.subagent_registry.register(
        SubagentDefinition(
            name="auditor",
            description="Review files",
            runner=run_auditor,
            allowed_tools=frozenset({"FileReadTool"}),
            max_tool_calls=3,
        )
    )
    state = {"runtime": runtime, "task": "review", "messages": []}
    events = []
    result = _call_registered_subagent_tool(
        state, events.append, "auditor", "report.txt"
    )

    assert result["agent"] == "auditor"
    assert result["evidence"] == ["report.txt"]
    assert state["agent_handoffs"][0]["to_agent"] == "auditor"
    assert [event["type"] for event in events] == ["handoff", "handoff_result"]


def test_idempotent_tool_retries_only_transient_failures(tmp_path: Path) -> None:
    calls = {"count": 0}

    def flaky(query: str):
        calls["count"] += 1
        if calls["count"] < 3:
            raise TimeoutError("request timed out")
        return {"ok": True, "query": query}

    tool = StructuredTool.from_function(
        name="WebSearchTool", func=flaky, description="Test retry tool"
    )
    runtime = RuntimeState(workspace=tmp_path, tool_retry_limit=2)
    retries = []
    result = execute_tool(runtime, tool, {"query": "demo"}, on_retry=retries.append)

    assert result["ok"] is True
    assert calls["count"] == 3
    assert len(retries) == 2
    assert retries[0]["error_type"] == "transient"
    assert retries[0]["retry_reason"] == "TimeoutError: request timed out"
    assert retries[0]["tool_call_id"] == "WebSearchTool-call"


def test_non_idempotent_tool_does_not_retry(tmp_path: Path) -> None:
    calls = {"count": 0}

    def write_file(file_path: str):
        calls["count"] += 1
        raise TimeoutError("timed out after write")

    tool = StructuredTool.from_function(
        name="FileWriteTool", func=write_file, description="Test write tool"
    )
    result = execute_tool(
        RuntimeState(workspace=tmp_path), tool, {"file_path": "a.txt"}
    )

    assert result["error_type"] == "transient"
    assert result["retryable"] is False
    assert result["retries"] == 0
    assert result["retry_stop_reason"] == "non_idempotent_side_effect"
    assert calls["count"] == 1


def test_retry_limit_returns_structured_stop_reason_and_trace_summary(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("mokioclaw.tools.execution.time.sleep", lambda _delay: None)

    def always_timeout(query: str):
        raise TimeoutError("upstream unavailable")

    runtime = RuntimeState(workspace=tmp_path, tool_retry_limit=1)
    trace = TraceRecorder(runtime, task="retry limit")
    events = []

    def writer(event):
        events.append(event)
        trace.record_custom_event(event)

    tool = StructuredTool.from_function(
        name="WebSearchTool", func=always_timeout, description="Test retry tool"
    )
    result = execute_tool(
        runtime,
        tool,
        {"query": "demo"},
        on_retry=writer,
        on_event=writer,
        tool_call_id="search-1",
    )
    writer(
        {
            "type": "tool_result",
            "tool_call_id": "search-1",
            "name": "WebSearchTool",
            "result": result,
        }
    )
    summary = trace.end(status="finished")

    assert result["error_type"] == "transient"
    assert result["retryable"] is False
    assert result["retries"] == 1
    assert result["retry_stop_reason"] == "retry_limit_reached"
    retry_events = [event for event in events if event["type"] == "tool_retry"]
    assert len(retry_events) == 1
    assert retry_events[0]["tool_call_id"] == "search-1"
    assert retry_events[0]["retry_reason"] == "TimeoutError: upstream unavailable"
    assert summary["tool_retries"] == 1
    assert summary["tool_retry_errors"] == {"transient": 1}
    assert summary["tool_error_types"] == {"transient": 1}
    assert summary["tool_retry_stops"]["retry_limit_reached"] == 1


def test_non_retryable_failures_and_unknown_categories_do_not_retry(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("mokioclaw.tools.execution.time.sleep", lambda _delay: None)
    calls = {"count": 0}

    def fail_with_unknown_outcome(query: str):
        calls["count"] += 1
        raise ToolExecutionError(
            "outcome_unknown", "request may have completed", retryable=True
        )

    tool = StructuredTool.from_function(
        name="WebSearchTool",
        func=fail_with_unknown_outcome,
        description="Test structured failure",
    )
    retries = []
    result = execute_tool(
        RuntimeState(workspace=tmp_path, tool_retry_limit=3),
        tool,
        {"query": "demo"},
        on_retry=retries.append,
    )

    assert calls["count"] == 1
    assert retries == []
    assert result["error_type"] == "outcome_unknown"
    assert result["retryable"] is False
    assert result["retry_stop_reason"] == "not_recoverable"


def test_structured_error_category_takes_precedence_over_legacy_message(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("mokioclaw.tools.execution.time.sleep", lambda _delay: None)
    tool = StructuredTool.from_function(
        name="WebSearchTool",
        func=lambda query: {
            "ok": False,
            "error": "timed out",
            "error_type": "permission_denied",
            "retryable": True,
        },
        description="Test explicit error category",
    )
    retries = []
    result = execute_tool(
        RuntimeState(workspace=tmp_path),
        tool,
        {"query": "demo"},
        on_retry=retries.append,
    )

    assert result["error_type"] == "permission_denied"
    assert result["retryable"] is False
    assert result["retry_stop_reason"] == "not_recoverable"
    assert retries == []


def test_tool_output_is_bounded(tmp_path: Path) -> None:
    tool = StructuredTool.from_function(
        name="FileReadTool",
        func=lambda: {"ok": True, "content": "x" * 500},
        description="Test read tool",
    )
    result = execute_tool(
        RuntimeState(workspace=tmp_path, context_output_limit_chars=40), tool, {}
    )
    assert result["output_truncated"] is True
    assert result["omitted_characters"] == 460
    assert len(result["content"]) < 100


def test_skill_discovery_loads_declared_scope_and_rejects_invalid(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".mokioclaw" / "skills" / "html-page"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: html-page\ndescription: Create HTML pages.\ntools:\n  - FileWriteTool\npermissions:\n  - filesystem_write\n---\nWrite accessible pages.\n",
        encoding="utf-8",
    )
    invalid_dir = tmp_path / ".mokioclaw" / "skills" / "broken"
    invalid_dir.mkdir()
    (invalid_dir / "SKILL.md").write_text("not frontmatter", encoding="utf-8")
    runtime = RuntimeState(workspace=tmp_path)

    skills = discover_skills(runtime)
    assert any(item["name"] == "html-page" and item["valid"] for item in skills)
    assert any(item["name"] == "broken" and not item["valid"] for item in skills)
    loaded = load_skill(runtime, "html-page")
    assert loaded["body"] == "Write accessible pages."
    assert runtime.active_skill_tools == {"FileWriteTool"}
    assert runtime.active_skill_permissions == {"filesystem_write"}
    assert (
        authorize_tool(runtime, "BashTool", {"command": "echo no"})["error_type"]
        == "permission_denied"
    )


def test_skill_parser_requires_required_frontmatter(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("---\nname: demo\n---\nbody", encoding="utf-8")
    with pytest.raises(ValueError, match="name, description"):
        parse_skill(skill)


def test_compact_session_history_keeps_recent_turns(tmp_path: Path) -> None:
    session = load_or_create_session(tmp_path)
    session["recent_turns"] = [
        {
            "turn": number,
            "role": "user",
            "route": "chat",
            "content": f"turn {number}",
            "summary": f"summary {number}",
        }
        for number in range(1, 7)
    ]
    save_session(tmp_path, session)

    result = compact_session_history(tmp_path, keep_recent=2)
    saved = load_or_create_session(tmp_path)
    assert result["compacted"] == 4
    assert result["kept"] == 2
    assert "summary 1" in saved["summary"]
    assert len(saved["recent_turns"]) == 2


class _StreamingModel:
    def stream(self, messages):
        yield AIMessageChunk(content="hel")
        yield AIMessageChunk(content="lo")


def test_streaming_model_emits_tokens_and_returns_assembled_message() -> None:
    events = []
    message = invoke_streaming(_StreamingModel(), [], writer=events.append, node="demo")
    assert message.content == "hello"
    assert [event["type"] for event in events] == [
        "llm_stream_start",
        "llm_token",
        "llm_token",
        "llm_stream_end",
    ]


def test_mcp_stdio_example_is_discovered_and_callable(tmp_path: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto")
    runtime.mcp_servers = [
        {
            "name": "example",
            "command": sys.executable,
            "args": [str(project / "examples" / "mcp" / "echo_server.py")],
            "cwd": str(project),
        }
    ]
    tools = build_mcp_tools(runtime)
    echo = next(tool for tool in tools if tool.name == "MCP_example_echo")
    result = execute_tool(runtime, echo, {"text": "hello"})
    assert result["ok"] is True
    assert result["content"] == ["echo: hello"]
