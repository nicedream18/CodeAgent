from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from textual import events
from textual.widgets import Static
from typer.testing import CliRunner

from mokioclaw.agents.registry import registered_subagents, run_subagent
from mokioclaw.cli.app import app
from mokioclaw.cli.tui import MokioClawTuiApp
from mokioclaw.cli.tui.approval import ApprovalModal
from mokioclaw.cli.tui.input import TaskInput
from mokioclaw.core.agent import create_runtime
from mokioclaw.core.agent_profiles import load_agent_profiles
from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.approval_rules import runtime_rules
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.core.transport_trace import TraceReader, transport_record
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.providers.payload_trace import payload_reference
from mokioclaw.providers.usage import normalize_usage


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def profile(path, *, name="reviewer", base="repoAgent", tools=None, prompt="Review only"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'name={json.dumps(name)}\ndescription="Test role"\nbase={json.dumps(base)}\nsystem_prompt={json.dumps(prompt)}\nallowed_tools={json.dumps(tools or ["FileReadTool"])}\nmax_tool_calls=3\n'
    )


def test_role_priority_pin_version_and_capability_ceiling(tmp_path):
    user = Path.home() / ".mokioclaw/agents/reviewer.toml"
    project = tmp_path / ".mokioclaw/agents/reviewer.toml"
    profile(user, prompt="User version")
    profile(project, prompt="Project version")
    runtime = create_runtime(tmp_path)
    assert runtime.agent_profiles["reviewer"]["system_prompt"] == "Project version"
    assert runtime.subagent_registry.get("reviewer").max_tool_calls == 3
    assert not runtime.subagent_registry.get("reviewer").background_safe
    pinned = runtime.agent_profiles["reviewer"]["version"]
    profile(project, tools=["BashTool"])
    assert runtime.agent_profiles["reviewer"]["version"] == pinned
    newer = create_runtime(tmp_path)
    assert newer.subagent_registry.get("reviewer") is None
    assert "exceeds base capability" in newer.agent_profile_diagnostics[0]["error"]
    assert newer.subagent_registry.get("codeAgent") is not None


def test_duplicate_roles_and_unknown_base_are_diagnosed(tmp_path):
    directory = tmp_path / ".mokioclaw/agents"
    profile(directory / "one.toml")
    profile(directory / "two.toml")
    profile(directory / "bad.toml", name="bad", base="not-a-runner")
    runtime = create_runtime(tmp_path)
    assert runtime.subagent_registry.get("reviewer") is None
    assert len(runtime.agent_profile_diagnostics) == 2


def test_profile_runner_receives_scope_prompt_and_restores_skill(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, subagent_registry=registered_subagents())
    base = runtime.subagent_registry.get("repoAgent")

    def runner(state, instruction, **kwargs):
        assert runtime.active_agent_tools == {"FileReadTool"}
        assert runtime.active_agent_prompt == "Pinned prompt"
        assert runtime.active_subagent_budget.limit == 3

        class Model:
            def invoke(self, messages):
                return AIMessage(content="reviewed")

        invoke_streaming(Model(), [], state=state, node="repoAgent")
        runtime.active_skill_name = "temporary"
        return {"summary": "reviewed", "evidence": []}

    runtime.subagent_registry.register(replace(base, runner=runner), replace=True)
    profile(tmp_path / ".mokioclaw/agents/reviewer.toml", prompt="Pinned prompt")
    load_agent_profiles(runtime)
    result = run_subagent({"task": "review", "runtime": runtime}, "reviewer", "inspect")
    assert result["ok"] and result["summary"] == "reviewed"
    assert runtime.active_agent_tools is None and runtime.active_agent_prompt == ""
    assert runtime.active_skill_name is None
    assert runtime.model_metrics["roles"]["reviewer"]["requests"] == 1


@pytest.mark.parametrize(
    ("usage", "metadata", "read", "created", "source"),
    [
        (
            {
                "input_tokens": 100,
                "output_tokens": 10,
                "total_tokens": 110,
                "input_token_details": {"cache_read": 70, "cache_creation": 3},
            },
            {},
            70,
            3,
            "usage_metadata",
        ),
        (
            {},
            {
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 80},
                }
            },
            80,
            None,
            "response_metadata.token_usage",
        ),
        (
            {},
            {
                "token_usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 0,
                }
            },
            0,
            None,
            "response_metadata.token_usage",
        ),
        (
            {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
            {},
            None,
            None,
            "usage_metadata",
        ),
    ],
)
def test_usage_cache_is_subset_not_extra(usage, metadata, read, created, source):
    message = AIMessage(content="中文", usage_metadata=usage or None, response_metadata=metadata)
    normalized = normalize_usage(message, 500)
    assert normalized.total_tokens == 110 and normalized.input_tokens == 100
    assert normalized.cache_read_tokens == read and normalized.cache_creation_tokens == created
    assert not normalized.estimated and normalized.source == source


def test_trace_payload_opt_in_redacts_and_bounds(tmp_path, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret-for-test-12345")
    runtime = RuntimeState(workspace=tmp_path)
    assert not payload_reference(runtime, {"test": True})
    runtime.settings = {"trace": {"include_model_payload": True}}
    reference = payload_reference(
        runtime,
        {
            "Authorization": "Bearer private",
            "text": "secret-for-test-12345",
            "nested": {"api_key": "private"},
            "long": "中" * 500000,
        },
    )
    path = tmp_path / reference["full_output_path"]
    text = path.read_text()
    assert "secret-for-test" not in text and "private" not in text
    assert reference["output_truncated"] and path.stat().st_size <= 1024 * 1024
    assert path.stat().st_mode & 0o777 == 0o600


def test_transport_trace_incremental_read_old_records_and_cli(tmp_path, monkeypatch):
    daemon = tmp_path / "daemon"
    monkeypatch.setenv("MOKIO_DAEMON_DIR", str(daemon))
    transport_record(
        daemon,
        "CLIENT→CORE",
        "rpc_received",
        request={"id": "request1", "method": "status", "params": {"token": "DO-NOT-LOG"}},
    )
    runtime = RuntimeState(workspace=tmp_path, run_id="run1")
    recorder = TraceRecorder(runtime, task="test")
    recorder.record_custom_event(
        {"type": "llm_stream_start", "node": "codeAgent", "stream_id": "stream1", "run_id": "run1"}
    )
    recorder.record_custom_event({"type": "llm_token", "stream_id": "stream1", "text": "部分中文"})
    recorder.record_custom_event(
        {"type": "llm_stream_end", "stream_id": "stream1", "status": "interrupted"}
    )
    reader = TraceReader()
    records = list(reader.read(tmp_path, daemon_root=daemon))
    assert len(records) >= 3 and not list(reader.read(tmp_path, daemon_root=daemon))
    assert "DO-NOT-LOG" not in (daemon / "transport/current.jsonl").read_text()
    llm = [r for _, r in records if r["layer"] == "llm"]
    assert all(r["run_id"] == "run1" and r["stream_id"] == "stream1" for r in llm)
    transport_record(
        daemon,
        "CORE→CLIENT",
        "rpc_response",
        request={"id": "request1"},
        response={"result": {"run_id": "run1"}},
    )
    assert len(list(reader.read(tmp_path, daemon_root=daemon))) == 1
    response = CliRunner().invoke(
        app, ["trace", "--workspace", str(tmp_path), "--layer", "llm", "--run-id", "run1", "--raw"]
    )
    assert (
        response.exit_code == 0
        and "stream1" in response.stdout
        and "rpc_received" not in response.stdout
    )


def write_skill(root, name="review"):
    path = root / ".mokioclaw/skills" / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: Review files\ntools: [FileReadTool]\npermissions: [filesystem_read]\n---\nAlways include SKILL_MARKER in the answer.\n"
    )


def test_explicit_skill_routes_to_workflow_and_is_visible_only_in_code_scope(tmp_path, monkeypatch):
    from mokioclaw.agents import code_agent
    from mokioclaw.graph import nodes

    write_skill(tmp_path)
    runtime = create_runtime(tmp_path, skill="review")
    assert (
        nodes.intent_router_node({"runtime": runtime, "task": "hello"})["intent_route"]
        == "workflow"
    )
    seen = []

    class Model:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            seen.extend(messages)
            assert runtime.active_skill_tools == {"FileReadTool"}
            return AIMessage(content="SKILL_MARKER reviewed")

    monkeypatch.setattr(code_agent, "create_model", lambda: Model())
    result = run_subagent({"task": "review", "runtime": runtime}, "codeAgent", "inspect")
    assert result["ok"] and any("SKILL_MARKER" in str(m.content) for m in seen)
    assert runtime.active_skill_tools is None


def test_tui_multiline_completion_skill_and_approval_scope(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, "help")
    calls = []

    def stream(task, **kwargs):
        calls.append((task, kwargs))
        return iter([])

    async def scenario():
        application = MokioClawTuiApp(workspace=tmp_path, stream_factory=stream)
        async with application.run_test(size=(120, 48)) as pilot:
            editor = application.query_one("#task-input", TaskInput)
            await editor._on_paste(events.Paste("first\nsecond"))
            await pilot.pause()
            assert editor.value == "first\nsecond" and calls == []
            await pilot.press("shift+enter")
            assert editor.value.endswith("\n")
            editor.value = "/sk"
            await pilot.pause()
            assert application.query_one("#command-completion", Static).display
            await pilot.press("tab")
            assert editor.value.startswith("/skill")
            await pilot.press("escape")
            editor.value = "/skill review inspect app.py"
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert calls[-1][0] == "inspect app.py" and calls[-1][1]["skill"] == "review"
            assert len([n for n, _ in application.command_candidates() if n == "help"]) == 1
            decisions = []
            application.push_screen(
                ApprovalModal(
                    ApprovalRequest("a", "write", "risk", rule_preview="file:app.py"),
                    str(tmp_path),
                    remember=True,
                ),
                decisions.append,
            )
            await pilot.pause()
            await pilot.click("#allow-session")
            await pilot.pause()
            assert decisions[0] == ApprovalDecision(True, scope="session")

    asyncio.run(scenario())


def test_stale_approval_cannot_write_or_save_rule(tmp_path):
    from mokioclaw.tools.execution import execute_tool
    from mokioclaw.tools.registry import build_tool

    runtime = RuntimeState(workspace=tmp_path, tool_policies={"FileWriteTool": "ask"})

    def handler(request):
        runtime.context_version += 1
        return ApprovalDecision(True, scope="project")

    runtime.approval_handler = handler
    result = execute_tool(
        runtime, build_tool(runtime, "FileWriteTool"), {"file_path": "a", "content": "bad"}
    )
    assert not result["ok"] and not (tmp_path / "a").exists()
    assert not runtime_rules(runtime).list_rules()["rules"]


def test_trace_failure_keeps_model_result(tmp_path, monkeypatch):
    import mokioclaw.core.trace as trace

    runtime = RuntimeState(workspace=tmp_path)
    recorder = TraceRecorder(runtime)

    def fail(*args, **kwargs):
        raise OSError("diagnostic disk unavailable")

    monkeypatch.setattr(trace, "DiagnosticArtifact", fail)

    class Model:
        def invoke(self, messages):
            return AIMessage(content="complete")

    result = invoke_streaming(
        Model(), [], state={"runtime": runtime}, writer=recorder.record_custom_event
    )
    assert result.content == "complete" and any("unavailable" in e for e in recorder.errors)


def test_explicit_skill_cannot_be_replaced(tmp_path):
    from mokioclaw.tools.skills import load_skill

    write_skill(tmp_path, "one")
    write_skill(tmp_path, "two")
    runtime = RuntimeState(workspace=tmp_path, explicit_skill="one")
    assert load_skill(runtime, "one")["ok"]
    assert not load_skill(runtime, "two")["ok"]
    assert runtime.active_skill_name == "one"


def test_shared_tool_loop_closes_planner_group_when_subagent_redirects(tmp_path):
    from langchain_core.messages import ToolMessage

    from mokioclaw.core.run_inbox import RunRedirected
    from mokioclaw.core.tool_loop import ToolConversation

    calls = [
        {"name": "CallSubagentTool", "id": "a", "args": {}},
        {"name": "FileWriteTool", "id": "b", "args": {}},
    ]

    class Model:
        def invoke(self, messages):
            return AIMessage(content="", tool_calls=calls)

    messages = []
    events = []
    loop = ToolConversation(
        messages,
        state={"runtime": RuntimeState(workspace=tmp_path)},
        writer=events.append,
        node="planner",
        invoke=invoke_streaming,
    )
    with pytest.raises(RunRedirected):
        for _response, tool_calls in loop.rounds(1, lambda: Model()):
            with loop.batch(tool_calls) as pending:
                for _call in pending:
                    raise RunRedirected()
    assert [m.tool_call_id for m in messages if isinstance(m, ToolMessage)] == ["a", "b"]
    assert all(
        e["result"]["status"] == "not_executed" for e in events if e["type"] == "tool_result"
    )


def test_transport_logs_rotation_quota_and_partial_line_reader(tmp_path):
    root = tmp_path / "transport"
    root.mkdir()
    for index in range(40):
        (root / f"old-{index}.jsonl").write_text("{}\n")
    transport_record(tmp_path, "CLIENT→CORE", "rpc_received", request={"id": "r"})
    assert len(list(root.glob("*.jsonl"))) <= 32
    path = root / "current.jsonl"
    reader = TraceReader()
    list(reader.read(daemon_root=tmp_path))
    with path.open("a") as stream:
        stream.write('{"type":"partial"')
    assert not list(reader.read(daemon_root=tmp_path))
    with path.open("a") as stream:
        stream.write("}\n")
    assert list(reader.read(daemon_root=tmp_path))[0][1]["type"] == "partial"
