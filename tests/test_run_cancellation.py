from __future__ import annotations

import asyncio
import json
import threading
import time
from io import BytesIO

import pytest
from langchain_core.messages import AIMessageChunk
from textual.widgets import Collapsible, Static
from typer.testing import CliRunner

from mokioclaw.core.approval import ApprovalRequest
from mokioclaw.core.daemon import AgentDaemon, ApprovalWaiter, RunRecord
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.state import RuntimeState


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


def test_cancel_rpc_is_idempotent_wakes_approval_and_preserves_terminal_state(tmp_path):
    daemon = AgentDaemon(tmp_path)
    record = RunRecord("run-test", tmp_path / "run-test")
    daemon.runs[record.run_id] = record
    waiter = ApprovalWaiter(ApprovalRequest("a1", "write", "file"))
    record.waiters["a1"] = waiter
    assert request(daemon, "cancel_run", run_id="missing")["error_type"] == "unknown_run"
    assert request(daemon, "cancel_run", run_id=record.run_id)["cancel_requested"]
    assert waiter.event.is_set() and not waiter.decision.approved
    request(daemon, "cancel_run", run_id=record.run_id)
    assert sum(row["event"]["type"] == "run_cancel_requested" for row in record.events) == 1
    assert record.status == "running"
    record.run_control.finish("interrupted")
    assert request(daemon, "cancel_run", run_id=record.run_id)["status"] == "interrupted"
    finished = RunRecord("done", tmp_path / "done")
    finished.status = finished.run_control.finish("finished")
    daemon.runs["done"] = finished
    assert request(daemon, "cancel_run", run_id="done")["status"] == "finished"
    assert not finished.run_control.cancel_event.is_set()
    daemon.executor.shutdown()


@pytest.mark.parametrize("session", [False, True])
def test_real_graph_cancel_during_router_has_one_trace_and_no_next_node(
    tmp_path, monkeypatch, session
):
    from mokioclaw.core import agent
    from mokioclaw.graph import nodes

    entered, closed = threading.Event(), threading.Event()
    token_received = threading.Event()
    control = RunControl()

    class SlowModel:
        async def astream(self, _messages):
            try:
                yield AIMessageChunk(content="部分路由文本")
                entered.set()
                await asyncio.sleep(30)
            finally:
                closed.set()

    monkeypatch.setattr(nodes, "create_model", lambda: SlowModel())
    monkeypatch.setattr(
        agent, "build_complex_workflow", lambda: pytest.fail("next workflow started")
    )
    received, failures = [], []

    def consume():
        try:
            stream = agent.stream_session_events if session else agent.stream_agent_events
            options = {"session_workspace" if session else "workspace": tmp_path}
            for event in stream("任务", run_control=control, approval_mode="deny", **options):
                received.append(event)
                payload = event.get("event", {})
                if payload.get("type") == "llm_token" and payload.get("text") == "部分路由文本":
                    token_received.set()
        except BaseException as exc:  # noqa: BLE001 — capture thread control signals for assertions
            failures.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert entered.wait(3)
    # The producer resuming after yield doesn't guarantee the execution thread
    # has consumed that token. Cancel after the actual streamed event instead.
    assert token_received.wait(3)
    control.cancel()
    worker.join(3)
    assert not worker.is_alive() and not failures
    assert closed.is_set()
    traces = list((tmp_path / ".mokioclaw/traces").iterdir())
    assert len(traces) == 1
    summary = json.loads((traces[0] / "summary.json").read_text())
    assert summary["status"] == "interrupted"
    checkpoint = json.loads((tmp_path / ".mokioclaw/checkpoints/checkpoint.json").read_text())
    assert checkpoint["status"] == "interrupted"
    rows = [json.loads(line) for line in (traces[0] / "events.jsonl").read_text().splitlines()]
    ends = [row["payload"]["payload"] for row in rows if row["type"] == "custom:llm_stream_end"]
    assert len(ends) == 1 and ends[0]["status"] == "interrupted"
    assert (tmp_path / ends[0]["full_output_path"]).read_text() == "部分路由文本"
    assert received[-1]["event"]["status"] == "interrupted"
    if session:
        saved = json.loads((tmp_path / ".mokioclaw/session/session.json").read_text())
        assert saved["last_run_status"] == "interrupted"
        assert not any(turn["role"] == "assistant" for turn in saved["recent_turns"])


def test_graph_failure_still_writes_failed_trace_and_checkpoint(tmp_path, monkeypatch):
    from mokioclaw.core import agent

    class BrokenGraph:
        def stream(self, *_args, **_kwargs):
            raise ValueError("broken graph")
            yield

    monkeypatch.setattr(agent, "build_entry_workflow", lambda: BrokenGraph())
    with pytest.raises(ValueError, match="broken graph"):
        list(agent.stream_agent_events("test", workspace=tmp_path))
    summaries = list(tmp_path.glob(".mokioclaw/traces/*/summary.json"))
    assert len(summaries) == 1 and json.loads(summaries[0].read_text())["status"] == "failed"


def test_background_child_control_is_independent_and_loop_is_closed(tmp_path, monkeypatch):
    from mokioclaw.agents.registry import SubagentDefinition, SubagentRegistry
    from mokioclaw.core.completion_budget import CompletionBudget
    from mokioclaw.graph import nodes

    parent = RunControl()
    seen = []

    class Manager:
        def submit(self, **kwargs):
            seen.append(kwargs["runner"])
            return {
                "ok": True,
                "subagent_run_id": "child",
                "status": "queued",
                "parent_run_id": kwargs["parent_run_id"],
                "handoff_id": kwargs["handoff_id"],
            }

    registry = SubagentRegistry()

    def child_runner(value, **kwargs):
        assert value.runtime.completion_budget is None
        return {"ok": True, "summary": "done"}

    registry.register(
        SubagentDefinition(
            name="demo",
            description="demo",
            legacy_runner=False,
            background_safe=True,
            allowed_tools=frozenset({"FileReadTool"}),
            max_tool_calls=1,
            runner=child_runner,
        )
    )
    runtime = RuntimeState(
        workspace=tmp_path,
        approval_mode="auto",
        run_control=parent,
        subagent_registry=registry,
        subagent_task_manager=Manager(),
        parent_run_id="parent",
        completion_budget=CompletionBudget(draining=True),
    )
    # The public delegation helper creates a private child RuntimeState.
    result = nodes._call_registered_subagent_tool(
        {"runtime": runtime, "task": "demo"},
        subagent_name="demo",
        instruction="test",
        writer=lambda _: None,
        run_in_background=True,
    )
    assert result["ok"]
    parent.cancel()
    child = seen[0](lambda _: None, threading.Event())
    assert child["ok"]
    assert runtime.completion_budget.draining


def test_live_daemon_cancel_foreground_preserves_background_work(tmp_path, monkeypatch):
    from mokioclaw.core import agent, daemon_client
    from mokioclaw.core.run_control import CURRENT_RUN
    from mokioclaw.core.subagent_tasks import current_daemon_context

    state_dir = tmp_path / "daemon"
    monkeypatch.setenv("MOKIO_DAEMON_DIR", str(state_dir))
    monkeypatch.setenv("MOKIO_DAEMON_WORKER", "")
    ready, release_child, child_finished = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    child_ids = []
    daemon = AgentDaemon(state_dir, port=0)
    server = threading.Thread(target=daemon.run, daemon=True)

    def fake_stream(_task, **_kwargs):
        manager, run_id, writer = current_daemon_context()

        def child(_writer, _cancel):
            assert release_child.wait(5)
            child_finished.set()
            return {"ok": True, "summary": "completed after parent cancellation"}

        accepted = manager.submit(
            parent_run_id=run_id,
            handoff_id="h1",
            agent="demo",
            instruction="test",
            runner=child,
            parent_writer=writer,
        )
        child_ids.append(accepted["subagent_run_id"])
        ready.set()
        while True:
            CURRENT_RUN.get().check()
            time.sleep(0.01)
            yield {"type": "custom_event", "event": {"type": "heartbeat"}}

    monkeypatch.setattr(agent, "stream_agent_events", fake_stream)
    received, errors = [], []

    def consume():
        try:
            received.extend(daemon_client.run_via_daemon("run", "test", options={}))
        except BaseException as exc:  # noqa: BLE001 — capture thread control signals for assertions
            errors.append(exc)

    consumer = threading.Thread(target=consume)
    server.start()
    try:
        deadline = time.monotonic() + 3
        while not (state_dir / "address.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        consumer.start()
        assert ready.wait(3)
        run_id = next(iter(daemon.runs))
        assert daemon_client.cancel_run(run_id)["ok"]
        consumer.join(3)
        assert not consumer.is_alive() and not errors
        assert daemon.runs[run_id].status == "interrupted"
        assert not daemon.subagent_manager.get(child_ids[0])["cancel_requested"]
        release_child.set()
        assert child_finished.wait(2)
        snapshot = daemon.subagent_manager.get(child_ids[0], wait_seconds=2)
        assert snapshot["status"] == "completed"
        assert received[0]["event"]["type"] == "run_started"
    finally:
        release_child.set()
        daemon_client.stop_daemon()
        server.join(3)
        consumer.join(3)


def test_daemon_cancellation_unblocks_approval_without_execution(tmp_path, monkeypatch):
    from mokioclaw.core import agent

    daemon = AgentDaemon(tmp_path / "daemon")

    def fake_stream(_task, *, approval_handler, **_kwargs):
        approval_handler(ApprovalRequest("a1", "write", "file"))
        pytest.fail("approval continued after cancellation")
        yield

    monkeypatch.setattr(agent, "stream_agent_events", fake_stream)
    run_id = daemon.start_run({"options": {"task": "test", "approval_mode": "inline"}})
    record = daemon.runs[run_id]
    deadline = time.monotonic() + 2
    while not record.waiters and time.monotonic() < deadline:
        time.sleep(0.01)
    assert record.waiters
    request(daemon, "cancel_run", run_id=run_id)
    deadline = time.monotonic() + 2
    while record.status == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert record.status == "interrupted" and not record.waiters
    assert json.loads((record.root / "run.json").read_text())["status"] == "interrupted"
    daemon.executor.shutdown()


def test_tui_real_ctrl_c_stream_state_partial_text_and_approval_release(tmp_path, monkeypatch):
    from mokioclaw.cli.tui import app as tui
    from mokioclaw.cli.tui.approval import ApprovalGate

    calls = []
    monkeypatch.setattr(tui, "cancel_run", lambda run: calls.append(run) or {"ok": True})

    async def scenario():
        app = tui.MokioClawTuiApp(workspace=tmp_path)
        async with app.run_test(size=(100, 32)) as pilot:
            app.running = True
            app.post_message(
                tui.AgentEventMessage(
                    {
                        "type": "custom_event",
                        "event": {"type": "run_started", "run_id": "run-test"},
                    }
                )
            )
            for payload in [
                {"type": "llm_stream_start"},
                {
                    "type": "llm_token",
                    "text": "[bold]开头[/bold]" + "中" * 5000 + "末尾",
                },
            ]:
                app.post_message(
                    tui.AgentEventMessage(
                        {
                            "type": "custom_event",
                            "event": {**payload, "stream_id": "s1", "node": "demo"},
                        }
                    )
                )
            await pilot.pause(0.1)
            assert "末尾" in str(app.query_one(".model-preview", Static).render())
            assert "[bold]开头[/bold]" in str(app.query_one(".model-full", Static).render())
            gate = ApprovalGate(ApprovalRequest("a1", "write", "file"))
            app.post_message(tui.ApprovalRequestedMessage(gate))
            await pilot.pause(0.05)
            await pilot.press("ctrl+c")
            await pilot.pause(0.1)
            assert calls == ["run-test"]
            assert gate.decision is not None and not gate.decision.approved
            assert "cancelling" in str(app.query_one("#status", Static).render())
            app.post_message(
                tui.AgentEventMessage(
                    {
                        "type": "custom_event",
                        "event": {
                            "type": "llm_stream_end",
                            "stream_id": "s1",
                            "node": "demo",
                            "status": "interrupted",
                        },
                    }
                )
            )
            app.post_message(tui.RunFinishedMessage("interrupted"))
            await pilot.pause(0.05)
            assert "interrupted" in str(app.query_one("#status", Static).render())
            assert any("interrupted" in card.title for card in app.query(Collapsible))
            assert not app.running and not app._llm_streams

    asyncio.run(scenario())


def test_cli_sigint_cancels_known_run_and_drains_final_status(monkeypatch):
    from mokioclaw.cli import app as cli

    calls = []

    def stream(*_args, **_kwargs):
        yield {"type": "custom_event", "event": {"type": "run_started", "run_id": "r1"}}
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "stream_agent_events", stream)
    monkeypatch.setattr(cli, "cancel_run", lambda run: calls.append(run) or {"ok": True})
    monkeypatch.setattr(
        cli,
        "iter_run_events",
        lambda *_args, **_kwargs: iter([{"type": "daemon_run_finished", "status": "interrupted"}]),
    )
    result = CliRunner().invoke(cli.app, ["test task"])
    assert result.exit_code == 130
    assert calls == ["r1"] and "cancelling" in result.output


def test_cli_approval_abort_becomes_run_interrupt(monkeypatch):
    import typer

    from mokioclaw.cli import app as cli

    def abort(*_args, **_kwargs):
        raise typer.Abort()

    monkeypatch.setattr(typer, "prompt", abort)
    with pytest.raises(KeyboardInterrupt):
        cli._inline_approval_handler(ApprovalRequest("a1", "write", "file"))


def test_tui_quit_does_not_cancel_run_and_unblocks_worker(tmp_path, monkeypatch):
    from mokioclaw.cli.tui import app as tui

    waiting, release = threading.Event(), threading.Event()
    calls = []
    monkeypatch.setattr(tui, "cancel_run", lambda run: calls.append(run))

    def stream(*_args, **_kwargs):
        yield {"type": "custom_event", "event": {"type": "run_started", "run_id": "r1"}}
        waiting.set()
        assert release.wait(3)
        yield {
            "type": "custom_event",
            "event": {"type": "run_finished", "status": "finished"},
        }

    async def scenario():
        app = tui.MokioClawTuiApp(initial_task="test", workspace=tmp_path, stream_factory=stream)
        async with app.run_test(size=(90, 30)) as pilot:
            # Wait for the worker handshake, independent of machine scheduling.
            assert await asyncio.to_thread(waiting.wait, 2)
            await pilot.press("ctrl+q")
        release.set()
        await asyncio.sleep(0.05)
        assert not calls

    try:
        asyncio.run(scenario())
    finally:
        release.set()


@pytest.mark.parametrize("mode", ["light", "strict"])
def test_resume_reads_original_checkpoint_before_new_start_snapshot(tmp_path, monkeypatch, mode):
    from mokioclaw.core import agent
    from mokioclaw.core.checkpoint import CheckpointManager, normalize_resume_task

    runtime = RuntimeState(workspace=tmp_path, checkpoint_mode=mode)
    original = {
        "runtime": runtime,
        "task": "ORIGINAL_TASK",
        "messages": [],
        "acceptance_criteria": ["ORIGINAL_CRITERION"],
        "todos": [{"id": "t1", "content": "finish", "status": "pending"}],
    }
    CheckpointManager(runtime, task="ORIGINAL_TASK").save(
        original, status="interrupted", latest_node="planner"
    )
    seen = {}

    class Workflow:
        def stream(self, inputs, **_kwargs):
            seen.update(inputs)
            yield ("updates", {"final": {"final_answer": "PASSED"}})

    monkeypatch.setattr(agent, "build_complex_workflow", lambda: Workflow())
    list(
        agent.stream_agent_events(
            resume_workspace=tmp_path, checkpoint_mode=mode, approval_mode="deny"
        )
    )
    assert normalize_resume_task(seen["task"]) == "ORIGINAL_TASK"
    assert seen["acceptance_criteria"] == ["ORIGINAL_CRITERION"]
    assert seen["todos"][0]["id"] == "t1"
    trace_rows = next(tmp_path.glob(".mokioclaw/traces/*/events.jsonl")).read_text().splitlines()
    assert normalize_resume_task(json.loads(trace_rows[0])["payload"]["task"]) == "ORIGINAL_TASK"
    assert len(list(tmp_path.glob(".mokioclaw/traces/*/summary.json"))) == 1


@pytest.mark.parametrize("ok", [True, False])
def test_late_cancel_reply_cannot_affect_next_tui_run(tmp_path, ok):
    from mokioclaw.cli.tui import app as tui
    from mokioclaw.cli.tui.approval import ApprovalGate

    async def scenario():
        app = tui.MokioClawTuiApp(workspace=tmp_path)
        async with app.run_test(size=(90, 30)) as pilot:
            app.running = True
            app.active_run_id = "new-run"
            app.query_one("#status", Static).update("running")
            gate = ApprovalGate(ApprovalRequest("a1", "write", "file"))
            app._approval_gates.append(gate)
            app.post_message(
                tui.AgentEventMessage({"type": "ui_cancel_result", "run_id": "old-run", "ok": ok})
            )
            await pilot.pause(0.03)
            assert gate.decision is None
            assert "running" == str(app.query_one("#status", Static).render())

    asyncio.run(scenario())
