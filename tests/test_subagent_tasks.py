from __future__ import annotations

import threading
import time
from pathlib import Path

from mokioclaw.agents.registry import (
    SubagentDefinition,
    SubagentRegistry,
    registered_subagents,
)
from mokioclaw.core.state import RuntimeState, SubagentToolBudget
from mokioclaw.core.subagent_tasks import SubagentTaskManager
from mokioclaw.graph.nodes import _agent_result_tool, _call_registered_subagent_tool
from mokioclaw.tools.execution import reserve_subagent_tool_call


def _wait_for(manager: SubagentTaskManager, run_id: str, expected: str) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = manager.get(run_id)
        if task and task.get("status") == expected:
            return task
        time.sleep(0.01)
    raise AssertionError(f"Subagent run did not reach {expected}")


def test_subagent_manager_persists_events_and_result(tmp_path: Path) -> None:
    manager = SubagentTaskManager(tmp_path, max_running=1, max_queued=2)
    try:
        accepted = manager.submit(
            parent_run_id="parent-1",
            handoff_id="handoff-1",
            agent="searchAgent",
            instruction="research",
            runner=lambda _writer, _cancel: {"ok": True, "summary": "done"},
        )
        run_id = accepted["subagent_run_id"]
        task = _wait_for(manager, run_id, "completed")
        assert task["result"]["summary"] == "done"
        assert manager.get(run_id) == task
        assert [item["seq"] for item in manager.events(run_id)] == list(
            range(1, len(manager.events(run_id)) + 1)
        )
    finally:
        manager.close()


def test_unfinished_persisted_task_is_marked_interrupted(tmp_path: Path) -> None:
    run_id = "subagent-012345abcdef"
    task_dir = tmp_path / run_id
    task_dir.mkdir(parents=True)
    (task_dir / "task.json").write_text(
        '{"subagent_run_id":"subagent-012345abcdef","parent_run_id":"p",'
        '"handoff_id":"h","agent":"searchAgent","status":"running"}',
        encoding="utf-8",
    )
    (task_dir / "events.jsonl").write_text(
        '{"seq":1,"event":{"type":"subagent_task_started"}}\n',
        encoding="utf-8",
    )
    manager = SubagentTaskManager(tmp_path)
    task = manager.get(run_id)
    assert task and task["status"] == "interrupted"
    assert manager.events(run_id, after_seq=1)[0]["event"]["type"] == "subagent_task_interrupted"


def test_subagent_queue_limit_and_queued_cancel(tmp_path: Path) -> None:
    manager = SubagentTaskManager(tmp_path, max_running=1, max_queued=1)
    started = threading.Event()
    release = threading.Event()

    def slow(_writer, _cancel):
        started.set()
        release.wait(2)
        return {"ok": True}

    try:
        first = manager.submit(
            parent_run_id="p",
            handoff_id="h1",
            agent="searchAgent",
            instruction="one",
            runner=slow,
        )
        assert started.wait(1)
        second = manager.submit(
            parent_run_id="p",
            handoff_id="h2",
            agent="searchAgent",
            instruction="two",
            runner=slow,
        )
        rejected = manager.submit(
            parent_run_id="p",
            handoff_id="h3",
            agent="searchAgent",
            instruction="three",
            runner=slow,
        )
        assert rejected["error_type"] == "subagent_queue_full"
        cancelled = manager.cancel(second["subagent_run_id"])
        assert cancelled["status"] == "cancelled"
        release.set()
        _wait_for(manager, first["subagent_run_id"], "completed")
        assert manager.get(second["subagent_run_id"])["status"] == "cancelled"
    finally:
        release.set()
        manager.close()


def test_subagent_cancel_event_reaches_running_runner(tmp_path: Path) -> None:
    manager = SubagentTaskManager(tmp_path, max_running=1)
    started = threading.Event()

    def cancellable(_writer, cancel_event):
        started.set()
        cancel_event.wait(2)
        return {"ok": False, "stop_reason": "cancelled"}

    try:
        accepted = manager.submit(
            parent_run_id="p",
            handoff_id="h",
            agent="searchAgent",
            instruction="cancel me",
            runner=cancellable,
        )
        run_id = accepted["subagent_run_id"]
        assert started.wait(1)
        cancelled = manager.cancel(run_id)
        assert cancelled["status"] in {"running", "cancelled"}
        assert _wait_for(manager, run_id, "cancelled")["cancel_requested"] is True
    finally:
        manager.close()


def test_read_only_subagents_run_concurrently(tmp_path: Path) -> None:
    manager = SubagentTaskManager(tmp_path, max_running=2)
    barrier = threading.Barrier(2)

    def overlap(_writer, _cancel):
        barrier.wait(timeout=2)
        return {"ok": True}

    try:
        tasks = [
            manager.submit(
                parent_run_id="p",
                handoff_id=f"h{i}",
                agent="searchAgent",
                instruction="research",
                runner=overlap,
            )
            for i in range(2)
        ]
        assert all(_wait_for(manager, task["subagent_run_id"], "completed") for task in tasks)
    finally:
        manager.close()


def test_cancelled_subagent_is_stopped_before_next_tool_call(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path)
    runtime.active_subagent_budget = SubagentToolBudget("searchAgent", "handoff", 4)
    runtime.subagent_cancel_event = threading.Event()
    runtime.subagent_cancel_event.set()
    result = reserve_subagent_tool_call(runtime, "WebSearchTool", "call-1")
    assert result is not None
    assert result["error_type"] == "cancelled"
    assert runtime.active_subagent_budget.used == 0


def test_background_search_is_retrieved_and_state_updates_apply_once(
    monkeypatch, tmp_path: Path
) -> None:
    from mokioclaw.graph import nodes

    manager = SubagentTaskManager(tmp_path / "runs", max_running=1)
    runtime = RuntimeState(workspace=tmp_path)
    runtime.subagent_task_manager = manager
    runtime.parent_run_id = "run-parent"
    runtime.subagent_registry = registered_subagents()
    state = {
        "runtime": runtime,
        "task": "research",
        "sources": [],
        "research_notes": "",
        "agent_handoffs": [],
    }
    events: list[dict] = []

    def fake_search(child_state, instruction, *, writer=None, max_loops=4):
        child_state["research_notes"] = f"notes for {instruction}"
        child_state["sources"] = [{"url": "https://example.test"}]
        return {"ok": True, "summary": "research complete"}

    monkeypatch.setattr(nodes, "run_search_agent", fake_search)
    try:
        accepted = _call_registered_subagent_tool(
            state,
            events.append,
            "searchAgent",
            "find sources",
            run_in_background=True,
        )
        assert accepted["status"] == "queued"
        run_id = accepted["subagent_run_id"]
        completed = _wait_for(manager, run_id, "completed")
        assert completed["result"]["summary"] == "research complete"

        first = _agent_result_tool(state, events.append, run_id)
        second = _agent_result_tool(state, events.append, run_id)
        assert first["status"] == "completed"
        assert second["status"] == "completed"
        assert state["research_notes"] == "notes for find sources"
        assert state["sources"] == [{"url": "https://example.test"}]
        assert sum(event.get("type") == "handoff_result" for event in events) == 1
    finally:
        manager.close()


def test_code_agent_cannot_be_started_in_background(tmp_path: Path) -> None:
    manager = SubagentTaskManager(tmp_path / "runs")
    runtime = RuntimeState(workspace=tmp_path)
    runtime.subagent_task_manager = manager
    runtime.parent_run_id = "run-parent"
    runtime.subagent_registry = registered_subagents()
    try:
        result = _call_registered_subagent_tool(
            {"runtime": runtime, "task": "edit"},
            lambda _event: None,
            "codeAgent",
            "edit file",
            run_in_background=True,
        )
        assert result["error_type"] == "background_not_allowed"
    finally:
        manager.close()


def test_background_safe_flag_still_requires_read_only_tool_scope(
    tmp_path: Path,
) -> None:
    manager = SubagentTaskManager(tmp_path / "runs")
    runtime = RuntimeState(workspace=tmp_path)
    runtime.subagent_task_manager = manager
    runtime.parent_run_id = "run-parent"
    registry = SubagentRegistry()
    registry.register(
        SubagentDefinition(
            "writer",
            "unsafe background writer",
            lambda *_args, **_kwargs: {"ok": True},
            frozenset({"FileWriteTool"}),
            2,
            background_safe=True,
        )
    )
    runtime.subagent_registry = registry
    try:
        result = _call_registered_subagent_tool(
            {"runtime": runtime, "task": "write"},
            lambda _event: None,
            "writer",
            "write a file",
            run_in_background=True,
        )
        assert result["error_type"] == "background_requires_read_only_tools"
    finally:
        manager.close()
