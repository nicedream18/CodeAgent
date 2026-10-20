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


def test_cancel_storm_physically_releases_pending_runners_and_terminal_cache(tmp_path):
    import weakref

    manager = SubagentTaskManager(tmp_path, max_running=1, max_queued=2)
    started, release = threading.Event(), threading.Event()

    def hold(writer, cancel):
        started.set()
        release.wait(10)
        return {"ok": True}

    first = manager.submit(
        parent_run_id="p", handoff_id="h", agent="searchAgent", instruction="hold", runner=hold
    )
    assert started.wait(2)
    ran = []

    class Runner:
        def __call__(self, writer, cancel):
            ran.append(True)
            return {"ok": True}

    refs, ids = [], []
    try:
        for i in range(1000):
            runner = Runner()
            refs.append(weakref.ref(runner))
            accepted = manager.submit(
                parent_run_id="p",
                handoff_id=str(i),
                agent="searchAgent",
                instruction="cancel",
                runner=runner,
            )
            assert accepted["ok"]
            ids.append(accepted["subagent_run_id"])
            assert manager.cancel(ids[-1])["status"] == "cancelled"
            del runner
            assert not manager._pending and not manager._runners
            assert len(manager._terminal) <= 128
            assert len(manager._tasks) <= 129
        assert all(ref() is None for ref in refs)
        assert manager.get(ids[0])["status"] == "cancelled"  # disk fallback after eviction
        assert manager.cancel(ids[0])["status"] == "cancelled"
        release.set()
        _wait_for(manager, first["subagent_run_id"], "completed")
        assert not ran
    finally:
        release.set()
        manager.close()


def test_event_tail_bounded_and_disk_replay_retains_monotonic_sequences(tmp_path):
    manager = SubagentTaskManager(tmp_path, max_running=1)

    def verbose(writer, cancel):
        for i in range(600):
            writer({"type": "observation", "i": i})
        return {"ok": True}

    try:
        accepted = manager.submit(
            parent_run_id="p",
            handoff_id="h",
            agent="searchAgent",
            instruction="events",
            runner=verbose,
        )
        run_id = accepted["subagent_run_id"]
        _wait_for(manager, run_id, "completed")
        task = manager._tasks[run_id]
        assert len(task.events) == 256
        assert task.event_sequence == 603
        all_events = manager.events(run_id)
        assert [e["seq"] for e in all_events] == list(range(1, 604))
        assert manager.events(run_id, after_seq=599) == all_events[599:]
        assert sum(e["event"]["type"] == "subagent_task_completed" for e in all_events) == 1
    finally:
        manager.close()


def test_persistence_failure_rejects_admission_without_runner_leak(tmp_path, monkeypatch):
    manager = SubagentTaskManager(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(manager, "_persist", fail)
    try:
        result = manager.submit(
            parent_run_id="p",
            handoff_id="h",
            agent="searchAgent",
            instruction="no",
            runner=lambda *a: {"ok": True},
        )
        assert result["error_type"] == "persistence_failed"
        assert not manager._runners and not manager._pending
        assert manager.get(result["subagent_run_id"])["status"] == "failed"
    finally:
        manager.close()


def test_completion_cancel_race_has_one_terminal_and_submit_after_close_rejected(tmp_path):
    manager = SubagentTaskManager(tmp_path, max_running=1)
    started, release = threading.Event(), threading.Event()

    def run(writer, cancel):
        started.set()
        release.wait(2)
        return {"ok": True}

    result = manager.submit(
        parent_run_id="p", handoff_id="h", agent="searchAgent", instruction="race", runner=run
    )
    run_id = result["subagent_run_id"]
    assert started.wait(2)
    barrier = threading.Barrier(2)

    def cancel():
        barrier.wait(timeout=2)
        manager.cancel(run_id)

    t = threading.Thread(target=cancel)
    t.start()
    barrier.wait(timeout=2)
    release.set()
    t.join(2)
    task = manager.wait(run_id)
    assert task["status"] in {"completed", "cancelled"}
    terminal = [
        e
        for e in manager.events(run_id)
        if e["event"]["type"] in {"subagent_task_completed", "subagent_task_cancelled"}
    ]
    assert len(terminal) == 1
    manager.close()
    rejected = manager.submit(
        parent_run_id="p", handoff_id="h", agent="searchAgent", instruction="late", runner=run
    )
    assert rejected["error_type"] == "daemon_stopping"


def test_shutdown_does_not_publish_second_terminal_for_late_runner(tmp_path):
    manager = SubagentTaskManager(tmp_path, max_running=1, shutdown_grace_seconds=0.01)
    started, release = threading.Event(), threading.Event()

    def run(writer, cancel):
        started.set()
        release.wait(2)
        writer({"type": "late"})
        return {"ok": True}

    accepted = manager.submit(
        parent_run_id="p", handoff_id="h", agent="searchAgent", instruction="late", runner=run
    )
    run_id = accepted["subagent_run_id"]
    assert started.wait(2)
    manager.close()
    assert manager.get(run_id)["status"] == "interrupted"
    release.set()
    for thread in manager._threads:
        thread.join(2)
    assert [e["event"]["type"] for e in manager.events(run_id)] == [
        "subagent_task_queued",
        "subagent_task_started",
        "subagent_task_interrupted",
    ]


def test_default_capacity_and_cancelled_jobs_never_dispatch(tmp_path):
    manager = SubagentTaskManager(tmp_path)
    release = threading.Event()
    barrier = threading.Barrier(5)
    dispatched = []

    def hold(writer, cancel):
        barrier.wait(3)
        release.wait(5)
        return {"ok": True}

    try:
        for i in range(4):
            assert manager.submit(
                parent_run_id="p", handoff_id=str(i), agent="a", instruction="hold", runner=hold
            )["ok"]
            # Let a worker dequeue before the next admission.
        barrier.wait(3)
        ids = []
        for i in range(16):
            r = manager.submit(
                parent_run_id="p",
                handoff_id=str(i),
                agent="a",
                instruction="pending",
                runner=lambda *a: dispatched.append(1) or {"ok": True},
            )
            assert r["ok"]
            ids.append(r["subagent_run_id"])
        rejected = manager.submit(
            parent_run_id="p",
            handoff_id="overflow",
            agent="a",
            instruction="no",
            runner=lambda *a: {"ok": True},
        )
        assert rejected["error_type"] == "subagent_queue_full"
        assert len(manager._running) == 4 and len(manager._pending) == 16
        for run_id in ids:
            assert manager.cancel(run_id)["status"] == "cancelled"
        assert not manager._pending and not manager._runners
        accepted = manager.submit(
            parent_run_id="p",
            handoff_id="new",
            agent="a",
            instruction="fresh",
            runner=lambda *a: {"ok": True},
        )
        assert accepted["ok"]
        release.set()
        assert manager.wait(accepted["subagent_run_id"], timeout_seconds=3)["status"] == "completed"
        assert not dispatched
    finally:
        release.set()
        manager.close()


def test_terminal_persistence_failure_publishes_failed_once_worker_remains_usable(
    tmp_path, monkeypatch
):
    manager = SubagentTaskManager(tmp_path, max_running=1)
    persist = manager._persist
    broken = []

    def fail_terminal_once(task):
        if task.status == "completed" and not broken:
            broken.append(1)
            raise OSError("full disk")
        return persist(task)

    monkeypatch.setattr(manager, "_persist", fail_terminal_once)
    try:
        first = manager.submit(
            parent_run_id="p",
            handoff_id="h",
            agent="a",
            instruction="first",
            runner=lambda *a: {"ok": True},
        )
        run_id = first["subagent_run_id"]
        task = manager.wait(run_id, timeout_seconds=3)
        assert task["status"] == "failed" and "persistence_failed" in task["error"]
        terminal = [
            x["event"]["type"]
            for x in manager.events(run_id)
            if x["event"]["type"] in {"subagent_task_failed", "subagent_task_completed"}
        ]
        assert terminal == ["subagent_task_failed"]
        second = manager.submit(
            parent_run_id="p",
            handoff_id="h",
            agent="a",
            instruction="second",
            runner=lambda *a: {"ok": True},
        )
        assert manager.wait(second["subagent_run_id"], timeout_seconds=3)["status"] == "completed"
    finally:
        manager.close()
