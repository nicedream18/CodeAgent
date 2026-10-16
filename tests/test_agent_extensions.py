from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool

from mokioclaw.agents.registry import (
    SubagentDefinition,
    SubagentRegistry,
    parse_subagent_configs,
)
from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.session import (
    compact_session_history,
    load_or_create_session,
    save_session,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_policy import authorize_tool, parse_tool_policies
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.graph.nodes import _call_registered_subagent_tool, context_monitor_node
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools import build_tools
from mokioclaw.tools.execution import ToolExecutionError, execute_tool
from mokioclaw.tools.mcp_tools import build_mcp_tools
from mokioclaw.tools.skills import discover_skills, load_skill, parse_skill


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
        {
            "runtime": RuntimeState(
                workspace=tmp_path,
                model_context_reserve_tokens=200,
                context_safety_tokens=0,
            ),
            "task": "demo",
        }
    )
    assert result["context_should_compress"] is True
    assert result["context_token_limit"] == 680


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
    assert result["evidence"] == [
        {
            "kind": "tool_event",
            "label": "report.txt",
            "reference": "report.txt",
            "details": "",
        }
    ]
    assert state["agent_handoffs"][0]["to_agent"] == "auditor"
    assert [event["type"] for event in events] == ["handoff", "handoff_result"]
    assert events[0]["handoff_id"] == events[1]["handoff_id"]


def test_subagent_budget_counts_invalid_calls_and_keeps_partial_updates(
    tmp_path: Path,
) -> None:
    effects = []
    tool = StructuredTool.from_function(
        name="TestTool",
        func=lambda value: effects.append(value) or {"ok": True},
        description="A test tool with one required argument.",
    )

    def runner(state, instruction, *, writer=None, max_loops=4):
        execute_tool(state["runtime"], tool, {}, on_event=writer)
        execute_tool(state["runtime"], tool, {"value": "must not run"}, on_event=writer)
        return {
            "ok": True,
            "summary": "partial work",
            "evidence": ["observed invalid request"],
            "state_updates": {"research_notes": "partial note"},
        }

    runtime = RuntimeState(workspace=tmp_path)
    runtime.subagent_registry = SubagentRegistry(
        {
            "auditor": SubagentDefinition(
                name="auditor",
                description="Budget test",
                runner=runner,
                allowed_tools=frozenset({"TestTool"}),
                max_tool_calls=1,
                state_update_fields=frozenset({"research_notes"}),
            )
        }
    )
    runtime.subagent_configs = {"auditor": {"max_tool_calls": 1}}
    state = {"task": "review", "runtime": runtime}
    events = []

    result = _call_registered_subagent_tool(state, events.append, "auditor", "inspect")

    assert effects == []
    assert result["ok"] is False
    assert result["stop_reason"] == "tool_budget_exhausted"
    assert result["metadata"]["tool_calls_used"] == 1
    assert state["research_notes"] == "partial note"
    budget_event = next(e for e in events if e["type"] == "subagent_budget_exhausted")
    assert budget_event["handoff_id"] == events[0]["handoff_id"]
    assert events[-1]["handoff_id"] == events[0]["handoff_id"]


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
    with pytest.raises(ValueError, match="description"):
        parse_skill(skill)


@pytest.mark.parametrize(
    ("frontmatter", "code"),
    [
        ("", "missing_frontmatter"),
        ("name: [broken\ndescription: x", "invalid_yaml"),
        ("name: demo", "missing_field"),
        ("name: demo\ndescription: x\ntools: FileWriteTool", "invalid_field_type"),
        ("name: demo\ndescription: x\npermissions: [unknown]", "unknown_permission"),
        ("name: demo\ndescription: x\ntools: [MissingTool]", "unknown_tool"),
        (
            "name: demo\ndescription: x\ntools: [FileWriteTool]",
            "missing_tool_permission",
        ),
    ],
)
def test_skill_parser_reports_structured_diagnostics(
    tmp_path: Path, frontmatter: str, code: str
) -> None:
    skill = tmp_path / "demo" / "SKILL.md"
    skill.parent.mkdir()
    skill.write_text(f"---\n{frontmatter}\n---\nbody\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        parse_skill(skill)
    assert exc.value.diagnostic["code"] == code
    assert exc.value.diagnostic["skill_name"]
    assert exc.value.diagnostic["path"] == str(skill)


def test_skill_empty_scope_denies_tools_and_failed_load_does_not_change_scope(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".mokioclaw" / "skills" / "empty"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: empty\ndescription: No extra capabilities.\ntools: []\npermissions: []\n---\nInstructions.\n",
        encoding="utf-8",
    )
    runtime = RuntimeState(workspace=tmp_path)
    runtime.active_skill_tools = {"GrepTool"}
    runtime.active_skill_permissions = {"filesystem_read"}
    failed = load_skill(runtime, "missing")
    assert failed["diagnostic"]["code"] == "not_found"
    assert runtime.active_skill_tools == {"GrepTool"}
    assert runtime.active_skill_permissions == {"filesystem_read"}

    loaded = load_skill(runtime, "empty")
    assert loaded["ok"] is True
    assert runtime.active_skill_tools == set()
    assert runtime.active_skill_permissions == set()
    assert (
        authorize_tool(runtime, "FileReadTool", {"file_path": "x"})["error_type"]
        == "permission_denied"
    )


def test_invalid_workspace_skill_shadows_user_skill(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    user_home = tmp_path / "home"
    workspace_skill = workspace / ".mokioclaw" / "skills" / "same" / "SKILL.md"
    user_skill = user_home / ".mokioclaw" / "skills" / "same" / "SKILL.md"
    workspace_skill.parent.mkdir(parents=True)
    user_skill.parent.mkdir(parents=True)
    workspace_skill.write_text("broken", encoding="utf-8")
    user_skill.write_text(
        "---\nname: same\ndescription: user fallback\n---\nUser body\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: user_home)
    runtime = RuntimeState(workspace=workspace)
    discovered = discover_skills(runtime)
    item = next(skill for skill in discovered if skill["name"] == "same")
    assert item["valid"] is False
    assert item["source"] == "workspace"
    assert load_skill(runtime, "same")["diagnostic"] == item["diagnostic"]


def test_skill_events_and_trace_summary_never_include_skill_body(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / ".mokioclaw" / "skills" / "logged"
    skill_dir.mkdir(parents=True)
    body = "PRIVATE SKILL BODY SHOULD NOT ENTER EVENT"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: logged\ndescription: Logging demo.\n---\n{body}\n",
        encoding="utf-8",
    )
    runtime = RuntimeState(workspace=tmp_path)
    trace = TraceRecorder(runtime, task="skill trace")
    events = []
    tool = next(tool for tool in build_tools(runtime) if tool.name == "LoadSkillTool")
    result = execute_tool(
        runtime, tool, {"name": "logged"}, on_event=events.append, tool_call_id="load-1"
    )
    assert result["ok"] is True
    loaded_event = next(event for event in events if event["type"] == "skill_loaded")
    assert loaded_event["tool_call_id"] == "load-1"
    assert loaded_event["skill_name"] == "logged"
    trace.record_custom_event(loaded_event)
    trace.record_custom_event(
        {
            "type": "tool_result",
            "node": "codeAgent",
            "name": "LoadSkillTool",
            "tool_call_id": "load-1",
            "result": result,
        }
    )
    failed = execute_tool(
        runtime,
        tool,
        {"name": "missing"},
        on_event=events.append,
        tool_call_id="load-2",
    )
    assert failed["ok"] is False
    failed_event = next(
        event for event in events if event["type"] == "skill_load_failed"
    )
    assert failed_event["diagnostic"]["code"] == "not_found"
    trace.record_custom_event(failed_event)
    summary = trace.end(status="finished")
    assert summary["skill_load_successes"] == 1
    assert summary["skill_load_failures"] == 1
    assert summary["skills_used"] == ["logged"]
    trace_text = (trace.root / "events.jsonl").read_text(encoding="utf-8")
    assert body not in trace_text


def test_codeagent_subagent_restores_complete_skill_scope(tmp_path: Path) -> None:
    from mokioclaw.agents.registry import run_subagent

    skill_dir = tmp_path / ".mokioclaw" / "skills" / "temporary"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: temporary\ndescription: temporary\ntools: [FileWriteTool]\npermissions: [filesystem_write]\n---\nTemp workflow\n",
        encoding="utf-8",
    )
    runtime = RuntimeState(workspace=tmp_path)
    runtime.active_skill_tools = {"GrepTool"}
    runtime.active_skill_permissions = {"filesystem_read"}
    runtime.active_skill_name = "outer"
    runtime.active_skill_source = "user"

    def runner(state, instruction, *, writer=None, max_loops=1):
        load_skill(state["runtime"], "temporary")
        return {"ok": True, "summary": "done"}

    registry = SubagentRegistry(
        {
            "codeAgent": SubagentDefinition(
                "codeAgent",
                "test",
                runner,
                frozenset({"LoadSkillTool", "FileWriteTool", "TodoUpdateTool"}),
                1,
            )
        }
    )
    runtime.subagent_registry = registry
    run_subagent({"runtime": runtime}, "codeAgent", "task")
    assert runtime.active_skill_tools == {"GrepTool"}
    assert runtime.active_skill_permissions == {"filesystem_read"}
    assert runtime.active_skill_name == "outer"
    assert runtime.active_skill_source == "user"


def test_codeagent_loads_skill_then_executes_declared_tool(
    monkeypatch, tmp_path: Path
) -> None:
    from mokioclaw.agents.code_agent import run_code_agent

    skill_dir = tmp_path / ".mokioclaw" / "skills" / "writer"
    skill_dir.mkdir(parents=True)
    body = "Use FileWriteTool to create the requested page."
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: writer\ndescription: Write one file.\ntools: [FileWriteTool]\npermissions: [filesystem_write]\n---\n{body}\n",
        encoding="utf-8",
    )

    class BoundModel:
        def __init__(self):
            self.calls = [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "LoadSkillTool",
                            "args": {"name": "writer"},
                            "id": "load",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "FileWriteTool",
                            "args": {
                                "file_path": "page.html",
                                "content": "<h1>Hi</h1>",
                            },
                            "id": "write",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Finished the page."),
            ]

        def invoke(self, messages):
            return self.calls.pop(0)

    class FakeModel:
        def bind_tools(self, tools):
            return BoundModel()

    monkeypatch.setattr("mokioclaw.agents.code_agent.create_model", lambda: FakeModel())
    runtime = RuntimeState(workspace=tmp_path)
    events = []
    result = run_code_agent(
        {"runtime": runtime, "task": "write a page"}, "create it", writer=events.append
    )
    assert result["summary"] == "Finished the page."
    assert (tmp_path / "page.html").read_text(encoding="utf-8") == "<h1>Hi</h1>"
    tool_messages = [
        message
        for message in result["messages"]
        if getattr(message, "name", "") == "LoadSkillTool"
    ]
    assert body in str(tool_messages[0].content)
    loaded = next(event for event in events if event["type"] == "skill_loaded")
    assert loaded["tool_call_id"] == "load"


def test_invalid_skill_scope_does_not_allow_a_write(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".mokioclaw" / "skills" / "invalid"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: invalid\ndescription: Missing write permission.\ntools: [FileWriteTool]\n---\nBad scope.\n",
        encoding="utf-8",
    )
    runtime = RuntimeState(workspace=tmp_path)
    runtime.active_skill_tools = {"LoadSkillTool"}
    runtime.active_skill_permissions = set()
    result = load_skill(runtime, "invalid")
    assert result["diagnostic"]["code"] == "missing_tool_permission"
    assert runtime.active_skill_tools == {"LoadSkillTool"}
    assert runtime.active_skill_permissions == set()
    write_tool = next(
        tool for tool in build_tools(runtime) if tool.name == "FileWriteTool"
    )
    denied = execute_tool(
        runtime, write_tool, {"file_path": "bad.txt", "content": "no"}
    )
    assert denied["error_type"] == "permission_denied"
    assert not (tmp_path / "bad.txt").exists()


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
        "context_budget",
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
