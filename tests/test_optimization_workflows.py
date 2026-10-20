from __future__ import annotations

import asyncio
import json
import threading
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from typer.testing import CliRunner

from mokioclaw.cli.app import app
from mokioclaw.cli.tui.input import TaskInput as Input
from mokioclaw.core.agent import stream_agent_events


class WorkflowModel:
    """A deterministic local model exercising real graphs and file tools."""

    def __init__(self):
        self.role, self.index = "router", 0

    def bind_tools(self, tools):
        names = {tool.name for tool in tools}
        self.role = (
            "planner"
            if "TodoWriteTool" in names
            else "code"
            if "FileWriteTool" in names
            else "verifier"
            if "BashTool" in names
            else "repo"
        )
        return self

    def invoke(self, messages):
        self.index += 1

        def calls(*specs):
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": uuid4().hex} for name, args in specs
                ],
            )

        if self.role == "router":
            return AIMessage(
                content=json.dumps(
                    {
                        "route": "workflow",
                        "confidence": 0.99,
                        "complexity": "simple",
                        "target_files": ["app.txt"],
                        "cross_module": False,
                    }
                )
            )
        if self.role == "repo":
            if self.index == 1:
                return calls(("FileReadTool", {"file_path": "app.txt"}))
            return AIMessage(content='{"summary":"app.txt is the only implementation target"}')
        if self.role == "planner":
            if self.index == 1:
                return calls(
                    (
                        "TodoWriteTool",
                        {
                            "todos": ["Update app.txt"],
                            "acceptance_criteria": ["app.txt contains after"],
                            "verification_commands": [],
                        },
                    ),
                    ("CallCodeAgentTool", {"instruction": "Update app.txt"}),
                )
            return AIMessage(content="Implemented; ready for verification")
        if self.role == "code":
            if self.index == 1:
                return calls(("FileReadTool", {"file_path": "app.txt"}))
            if self.index == 2:
                return calls(
                    (
                        "FileEditTool",
                        {
                            "file_path": "app.txt",
                            "old_text": "before",
                            "new_text": "after",
                        },
                    ),
                    ("TodoUpdateTool", {"todo_id": "todo-1", "status": "completed"}),
                )
            return AIMessage(content="Updated app.txt")
        if self.index == 1:
            return calls(("FileReadTool", {"file_path": "app.txt"}))
        assert "after" in str(messages[-1].content)
        return AIMessage(
            content='{"passed":true,"reason":"actual file verified","checks":[{"name":"file","passed":true,"detail":"after"}]}'
        )


@pytest.fixture
def local_models(monkeypatch):
    from mokioclaw.agents import code_agent, repo_agent, test_agent
    from mokioclaw.graph import nodes

    for module in (nodes, code_agent, repo_agent, test_agent):
        monkeypatch.setattr(module, "create_model", WorkflowModel)


@pytest.mark.parametrize("mode", ["full", "light", "auto"])
def test_real_workflow_modes_produce_same_verified_file(tmp_path, local_models, mode):
    (tmp_path / "app.txt").write_text("before")
    events = list(
        stream_agent_events(
            "将 app.txt 的 before 改成 after",
            workspace=tmp_path,
            workflow_mode=mode,
            approval_mode="deny",
        )
    )
    assert (tmp_path / "app.txt").read_text() == "after"
    terminal = [
        row["event"]
        for row in events
        if row.get("type") == "custom_event" and row["event"].get("type") == "run_finished"
    ][-1]
    assert terminal["status"] == "finished"
    expected = "full" if mode == "full" else "light"
    assert any(row.get("event", {}).get("mode") == expected for row in events)
    metrics = terminal["model_metrics"]
    assert metrics["roles"]["verifier"]["requests"] == 2
    assert metrics["roles"]["testAgent"]["requests"] == 2
    assert ("planner" in metrics["roles"]) == (mode == "full")
    assert metrics["estimated"]
    summaries = list((tmp_path / ".mokioclaw/traces").glob("*/summary.json"))
    assert len(summaries) == 1
    assert json.loads(summaries[0].read_text())["model_metrics"]["requests"] == metrics["requests"]


def test_light_verification_failure_escalates_once_preserving_completed_edit(
    tmp_path, local_models, monkeypatch
):
    from mokioclaw.graph import light

    (tmp_path / "app.txt").write_text("before")
    visits = []

    def initial_failure(state):
        visits.append("light-verifier")
        return {"passed": False, "attempts": 1}

    monkeypatch.setattr(light, "verifier_node", initial_failure)
    events = list(
        stream_agent_events(
            "update app.txt",
            workspace=tmp_path,
            workflow_mode="light",
            approval_mode="deny",
        )
    )
    assert visits == ["light-verifier"]
    assert (tmp_path / "app.txt").read_text() == "after"
    upgrades = [row for row in events if row.get("event", {}).get("upgraded")]
    assert len(upgrades) == 1
    journals = list((tmp_path / ".mokioclaw/changes").glob("*/manifest.json"))
    assert len(json.loads(journals[0].read_text())["changes"]) == 1
    custom = [row["event"] for row in events if row.get("type") == "custom_event"]
    counts = {
        name: [e["stream_id"] for e in custom if e.get("type") == name]
        for name in ("llm_stream_start", "llm_request", "llm_stream_end")
    }
    assert counts["llm_stream_start"] == counts["llm_request"] == counts["llm_stream_end"]
    assert len(set(counts["llm_request"])) == custom[-1]["model_metrics"]["requests"]


def test_budget_stop_persists_failed_reason_without_model_request(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    monkeypatch.setenv("MOKIO_RUN_TOKEN_LIMIT", "1")
    model = WorkflowModel()
    monkeypatch.setattr(nodes, "create_model", lambda: model)
    events = list(stream_agent_events("task", workspace=tmp_path, approval_mode="deny"))
    assert model.index == 0
    assert any(
        row.get("event", {}).get("stop_reason") == "token_budget_exhausted" for row in events
    )
    assert events[-1]["event"]["status"] == "failed"


def test_upgraded_graph_failure_keeps_one_end_per_stream_in_trace(
    tmp_path, local_models, monkeypatch
):
    from langchain_core.messages import AIMessage, HumanMessage

    from mokioclaw.core.context_budget import ContextBudgetError
    from mokioclaw.graph import light, nodes, workflow
    from mokioclaw.orchestration.runtime import single_step_workflow
    from mokioclaw.providers.openai_provider import invoke_streaming

    class Fixed:
        def invoke(self, messages):
            return AIMessage(content="partial progress")

    def failed_full(state):
        invoke_streaming(
            Fixed(), [HumanMessage(content="inspect")], state=state,
            writer=nodes._get_writer(), node="upgraded_test",
        )
        raise ContextBudgetError("controlled post-generation failure")

    def build_failed_full():
        return single_step_workflow("failed_full", failed_full)

    (tmp_path / "app.txt").write_text("before")
    monkeypatch.setattr(light, "verifier_node", lambda _: {"passed": False})
    monkeypatch.setattr(workflow, "build_complex_workflow", build_failed_full)
    events = []
    with pytest.raises(ContextBudgetError, match="controlled"):
        for event in stream_agent_events(
            "update app.txt", workspace=tmp_path, workflow_mode="light", approval_mode="deny"
        ):
            events.append(event)
    trace = next((tmp_path / ".mokioclaw/traces").glob("*/events.jsonl"))
    rows = [json.loads(line) for line in trace.read_text().splitlines()]
    starts = [r["stream_id"] for r in rows if r["type"] == "custom:llm_stream_start"]
    ends = [r["stream_id"] for r in rows if r["type"] == "custom:llm_stream_end"]
    assert len(starts) == len(set(starts)) == len(ends) == len(set(ends))
    assert set(starts) == set(ends)
    custom = [e["event"] for e in events if e.get("type") == "custom_event"]
    assert custom[-1]["status"] == "failed"
    requests = [e["stream_id"] for e in custom if e.get("type") == "llm_request"]
    assert len(requests) == custom[-1]["model_metrics"]["requests"]


def test_cli_new_commands_and_tui_mode_flag(monkeypatch, tmp_path):
    import mokioclaw.core.daemon_client as client
    from mokioclaw.cli import tui

    captured = {}
    monkeypatch.setattr(
        client,
        "submit_run_message",
        lambda run_id, message_id, text: {"ok": True, "status": "queued", "text": text},
    )
    runner = CliRunner()
    assert runner.invoke(app, ["send", "run-test", "补充要求"]).exit_code == 0

    class FakeApp:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def run(self):
            return None

    monkeypatch.setattr(tui, "MokioClawTuiApp", FakeApp)
    result = runner.invoke(app, ["tui", "--workspace", str(tmp_path), "--workflow-mode", "light"])
    assert result.exit_code == 0
    assert captured["workflow_mode"] == "light"


def test_tui_accepts_correction_while_running_and_displays_queued_state(tmp_path, monkeypatch):
    import mokioclaw.cli.tui.app as tui

    received = []
    started, release = threading.Event(), threading.Event()

    def stream(task, **kwargs):
        yield {
            "type": "custom_event",
            "event": {"type": "run_started", "run_id": "run-active"},
        }
        started.set()
        release.wait(5)
        yield {
            "type": "custom_event",
            "event": {"type": "run_finished", "status": "finished"},
        }

    monkeypatch.setattr(
        tui,
        "submit_run_message",
        lambda run_id, message_id, text: (
            received.append((run_id, text)) or {"ok": True, "status": "queued", "text": text}
        ),
    )

    async def scenario():
        application = tui.MokioClawTuiApp(workspace=tmp_path, stream_factory=stream)
        async with application.run_test() as pilot:
            application.start_task("task")
            for _ in range(100):
                await pilot.pause(0.01)
                if started.is_set() and application.active_run_id:
                    break
            field = application.query_one("#task-input", Input)
            assert not field.disabled
            field.value = "不要修改数据库"
            await pilot.press("enter")
            for _ in range(100):
                await pilot.pause(0.01)
                if received:
                    break
            assert received == [("run-active", "不要修改数据库")]
            release.set()
            await pilot.pause(0.1)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
