from __future__ import annotations

from pathlib import Path

from langchain_core.tools import StructuredTool

from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_policy import authorize_tool, parse_bash_patterns
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import ToolRegistry, ToolSpec, build_read_only_tools, build_tools, tool_spec_for


def test_registry_profiles_share_metadata_and_keep_agent_tool_sets(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path)
    execution_names = {tool.name for tool in build_tools(runtime)}
    read_only_names = {tool.name for tool in build_read_only_tools(runtime)}

    assert {"FileWriteTool", "FileEditTool", "LoadSkillTool"}.issubset(execution_names)
    assert "WebSearchTool" not in execution_names
    assert {"FileReadTool", "GrepTool", "WebSearchTool"}.issubset(read_only_names)
    assert "FileWriteTool" not in read_only_names
    assert tool_spec_for("FileWriteTool").capabilities == frozenset({"filesystem_write"})
    assert tool_spec_for("WebSearchTool").idempotent is True


def test_tool_registry_rejects_name_mismatch_and_duplicate() -> None:
    registry = ToolRegistry()
    tool = StructuredTool.from_function(name="SampleTool", func=lambda value: value, description="sample")
    spec = ToolSpec("SampleTool", None, frozenset({"execution"}), "sample")
    registry.register(tool, spec)
    assert registry.get("SampleTool") is tool
    assert "value" in registry.spec("SampleTool").input_schema["properties"]
    assert registry.tool_schemas("execution")[0]["name"] == "SampleTool"

    try:
        registry.register(tool, spec)
    except ValueError as exc:
        assert "duplicate" in str(exc)
    else:
        raise AssertionError("duplicate registration should fail")


def test_invalid_args_are_rejected_before_permission_prompt(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path)
    runtime.tool_policies["FileWriteTool"] = "ask"
    requests = []
    runtime.approval_handler = lambda request: requests.append(request) or ApprovalDecision(True)
    tool = StructuredTool.from_function(
        name="FileWriteTool",
        func=lambda file_path, content: {"ok": True},
        description="write",
    )

    result = execute_tool(runtime, tool, {"file_path": "page.html"}, tool_call_id="call-invalid")

    assert result["ok"] is False
    assert result["error_type"] == "invalid_input"
    assert requests == []


def test_approval_events_are_correlated_to_single_tool_call(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies["FileWriteTool"] = "ask"
    requests = []
    runtime.approval_handler = lambda request: requests.append(request) or ApprovalDecision(True)
    events = []
    tool = StructuredTool.from_function(
        name="FileWriteTool",
        func=lambda file_path, content: {"ok": True, "path": file_path},
        description="write",
    )

    result = execute_tool(
        runtime,
        tool,
        {"file_path": "page.html", "content": "hello"},
        on_event=events.append,
        tool_call_id="call-write-1",
    )

    assert result["ok"] is True
    assert requests[0].tool_call_id == "call-write-1"
    assert [event["type"] for event in events] == ["permission_requested", "permission_granted"]
    assert all(event["tool_call_id"] == "call-write-1" for event in events)


def test_rejected_approval_never_invokes_tool(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies["FileWriteTool"] = "ask"
    runtime.approval_handler = lambda _request: ApprovalDecision(False, "no write")
    executions = []
    events = []
    tool = StructuredTool.from_function(
        name="FileWriteTool",
        func=lambda file_path, content: executions.append(file_path) or {"ok": True},
        description="write",
    )

    result = execute_tool(
        runtime,
        tool,
        {"file_path": "page.html", "content": "hello"},
        on_event=events.append,
        tool_call_id="call-write-rejected",
    )

    assert result["error"] == "no write"
    assert executions == []
    assert [event["type"] for event in events] == ["permission_requested", "permission_denied"]


def test_bash_policy_precedence_deny_then_outside_ask_then_allow(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.bash_deny_patterns = [r"\bdeploy\b"]
    runtime.bash_allow_patterns = [r"pip install"]
    approvals = []
    runtime.approval_handler = lambda request: approvals.append(request) or ApprovalDecision(True)

    denied = authorize_tool(runtime, "BashTool", {"command": "deploy now"}, tool_call_id="deny-1")
    assert denied["error_type"] == "permission_denied"
    assert approvals == []

    outside = authorize_tool(runtime, "BashTool", {"command": "cat /etc/hosts"}, tool_call_id="outside-1")
    assert outside is None
    assert approvals[-1].tool_call_id == "outside-1"
    assert "outside the workspace" in approvals[-1].risk_reason

    allowed = authorize_tool(runtime, "BashTool", {"command": "pip install demo"}, tool_call_id="allow-1")
    assert allowed is None
    assert len(approvals) == 1


def test_bash_policy_patterns_are_validated() -> None:
    assert parse_bash_patterns('["rm\\\\s+-rf"]', variable="MOKIO_BASH_DENY_PATTERNS") == [r"rm\s+-rf"]
    try:
        parse_bash_patterns('["["]', variable="MOKIO_BASH_DENY_PATTERNS")
    except ValueError as exc:
        assert "invalid regular expression" in str(exc)
    else:
        raise AssertionError("invalid regex should fail at configuration time")


def test_approval_preview_redacts_secrets(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies["WebSearchTool"] = "ask"
    requests = []
    runtime.approval_handler = lambda request: requests.append(request) or ApprovalDecision(False)

    authorize_tool(runtime, "WebSearchTool", {"query": "search", "api_key": "secret-value"})

    assert "secret-value" not in requests[0].command
    assert "[redacted]" in requests[0].command


def test_unknown_tool_is_fail_closed(tmp_path: Path) -> None:
    result = authorize_tool(RuntimeState(workspace=tmp_path), "MissingTool", {})
    assert result["error_type"] == "permission_denied"
    assert result["retryable"] is False
