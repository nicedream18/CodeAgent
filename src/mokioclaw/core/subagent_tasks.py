from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_DAEMON_CONTEXT: ContextVar[tuple[Any, str, Callable[[dict[str, Any]], Any]] | None] = ContextVar(
    "mokioclaw_daemon_subagent_context", default=None
)


def set_daemon_context(manager: Any, run_id: str, writer: Callable[[dict[str, Any]], Any]) -> Token:
    return _DAEMON_CONTEXT.set((manager, run_id, writer))


def reset_daemon_context(token: Token) -> None:
    _DAEMON_CONTEXT.reset(token)


def current_daemon_context() -> tuple[Any, str, Callable[[dict[str, Any]], Any]] | None:
    return _DAEMON_CONTEXT.get()


@dataclass
class SubagentTask:
    subagent_run_id: str
    parent_run_id: str
    handoff_id: str
    agent: str
    instruction: str
    status: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    cancel_requested: bool = False
    events: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=256))
    event_sequence: int = 0
    persistence_error: str = ""
    terminal_published: bool = False
    condition: threading.Condition = field(default_factory=threading.Condition)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class SubagentTaskManager:
    """Bounded removable FIFO; admission, dequeue, cancel and finish share a lock."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        max_running: int = 4,
        max_queued: int = 16,
        shutdown_grace_seconds: float = 5.0,
    ) -> None:
        if max_running < 1 or max_queued < 1:
            raise ValueError("Subagent capacities must be positive")
        self.root = runs_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_running, self.max_queued = max_running, max_queued
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._tasks: dict[str, SubagentTask] = {}
        self._runners: dict[str, tuple[Callable[..., dict[str, Any]], Any]] = {}
        self._pending: OrderedDict[str, None] = OrderedDict()
        self._terminal: OrderedDict[str, None] = OrderedDict()
        self._running: set[str] = set()
        self._accepting, self._stopping, self._started = True, False, False
        self._threads: list[threading.Thread] = []
        self._mark_orphaned_tasks()

    def start(self) -> None:
        with self._lock:
            if self._started or self._stopping:
                return
            self._started = True
            for index in range(self.max_running):
                thread = threading.Thread(
                    target=self._worker, name=f"mokioclaw-subagent-{index}", daemon=True
                )
                self._threads.append(thread)
                thread.start()

    def _mark_orphaned_tasks(self) -> None:
        for path in self.root.glob("*/task.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    continue
                if payload.get("status") not in {"queued", "running"}:
                    continue
                payload.update(
                    status="interrupted",
                    error="daemon exited before this background task completed",
                )
                temporary = path.with_suffix(".tmp")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temporary.replace(path)
                event_path = path.parent / "events.jsonl"
                seq = 0
                if event_path.exists():
                    with event_path.open(encoding="utf-8") as handle:
                        for line in handle:
                            try:
                                seq = max(seq, int(json.loads(line).get("seq", 0)))
                            except (ValueError, TypeError):
                                continue
                event = {
                    "type": "subagent_task_interrupted",
                    "status": "interrupted",
                    **{
                        k: payload.get(k, "")
                        for k in ("subagent_run_id", "parent_run_id", "handoff_id")
                    },
                    "error": payload["error"],
                }
                with event_path.open("a", encoding="utf-8") as handle:
                    handle.write(
                        json.dumps({"seq": seq + 1, "event": event}, ensure_ascii=False) + "\n"
                    )
            except (OSError, ValueError, TypeError):
                # Startup remains available; queries retain the on-disk diagnostic.
                continue

    def stop_accepting(self) -> None:
        with self._lock:
            self._accepting = False

    def submit(
        self,
        *,
        parent_run_id: str,
        handoff_id: str,
        agent: str,
        instruction: str,
        runner: Callable[[Callable[[dict[str, Any]], None], threading.Event], dict[str, Any]],
        parent_writer: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        self.start()
        with self._condition:
            if not self._accepting:
                return {
                    "ok": False,
                    "error_type": "daemon_stopping",
                    "message": "daemon is stopping",
                }
            if len(self._pending) >= self.max_queued:
                return {
                    "ok": False,
                    "error_type": "subagent_queue_full",
                    "message": "Subagent queue is full",
                }
            run_id = f"subagent-{uuid4().hex[:12]}"
            task = SubagentTask(run_id, parent_run_id, handoff_id, agent, instruction)
            self._tasks[run_id] = task
            try:
                self._persist(task)
                self._append(task, {"type": "subagent_task_queued", **self._ids(task)}, strict=True)
            except OSError as exc:
                task.error = f"persistence_failed: {type(exc).__name__}"
                self._finish(task, "failed", error=task.error)
                return {
                    "ok": False,
                    "error_type": "persistence_failed",
                    "message": task.error,
                    **self._ids(task),
                }
            self._pending[run_id] = None
            self._runners[run_id] = runner, parent_writer
            self._notify_parent(
                task,
                parent_writer,
                {"type": "subagent_task_queued", **self._ids(task), "agent": agent},
            )
            self._condition.notify_all()
            return {"ok": True, "status": "queued", **self._ids(task)}

    def _worker(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: bool(self._pending) or self._stopping)
                if self._stopping:
                    return
                run_id, _ = self._pending.popitem(last=False)
                runner, parent_writer = self._runners.pop(run_id)
                task = self._tasks[run_id]
                # Cancellation and dequeue are indivisible. A cancelled queued
                # runner is physically removed and can never reach this point.
                task.status = "running"
                self._running.add(run_id)
                try:
                    self._persist(task)
                except OSError as exc:
                    self._running.discard(run_id)
                    self._finish(
                        task,
                        "failed",
                        error=f"persistence_failed: {type(exc).__name__}",
                        writer=parent_writer,
                    )
                    continue
                started = {"type": "subagent_task_started", **self._ids(task), "agent": task.agent}
                self._append(task, started)
                self._notify_parent(task, parent_writer, started)

            def child_writer(event: dict[str, Any], task: SubagentTask = task) -> None:
                with self._lock:
                    if task.status not in TERMINAL:
                        self._append(task, event)

            try:
                result = runner(child_writer, task.cancel_event)
                with self._lock:
                    self._finish(
                        task,
                        "cancelled"
                        if task.cancel_requested
                        else "completed"
                        if result.get("ok")
                        else "failed",
                        result=result,
                        writer=parent_writer,
                    )
            except BaseException as exc:  # noqa: BLE001 - a task control signal must never kill a worker or parent
                with self._lock:
                    self._finish(
                        task,
                        "cancelled" if task.cancel_requested else "failed",
                        error=f"{type(exc).__name__}: {exc}",
                        writer=parent_writer,
                    )
            finally:
                with self._condition:
                    self._running.discard(run_id)
                    self._condition.notify_all()
                # Do not retain the last runner closure while idle.
                del runner, parent_writer, task, child_writer

    @staticmethod
    def _ids(task: SubagentTask) -> dict[str, str]:
        return {
            "subagent_run_id": task.subagent_run_id,
            "parent_run_id": task.parent_run_id,
            "handoff_id": task.handoff_id,
        }

    def _finish(
        self,
        task: SubagentTask,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        writer: Any = None,
    ) -> bool:
        # Caller owns scheduler lock, including shutdown and completion races.
        if task.terminal_published:
            return False
        with task.condition:
            if task.persistence_error:
                status, error = "failed", task.persistence_error
            task.status, task.result, task.error = status, result, error
            try:
                self._persist(task)
            except OSError as exc:
                task.status, task.error = "failed", f"persistence_failed: {type(exc).__name__}"
                task.persistence_error = task.error
                try:
                    self._persist(task)
                except OSError:
                    pass
            task.terminal_published = True
            event = {
                "type": f"subagent_task_{task.status}",
                **self._ids(task),
                "status": task.status,
                "result": result,
                "error": task.error,
            }
            self._append(task, event)
            self._notify_parent(
                task,
                writer,
                {
                    **event,
                    "ok": task.status == "completed",
                    "summary": (result or {}).get("summary", ""),
                },
            )
            self._terminal[task.subagent_run_id] = None
            while len(self._terminal) > 128:
                old, _ = self._terminal.popitem(last=False)
                self._tasks.pop(old, None)
            task.condition.notify_all()
        self._condition.notify_all()
        return True

    def _append(self, task: SubagentTask, event: dict[str, Any], *, strict: bool = False) -> int:
        with task.condition:
            task.event_sequence += 1
            item = {"seq": task.event_sequence, "event": event}
            task.events.append(item)
            try:
                path = self.root / task.subagent_run_id / "events.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            except OSError as exc:
                task.persistence_error = f"event_persistence_failed: {type(exc).__name__}"
                item["event"] = {**event, "persistence_error": task.persistence_error}
                if strict:
                    raise
            task.condition.notify_all()
            return task.event_sequence

    def _notify_parent(self, task: SubagentTask, writer: Any, event: dict[str, Any]) -> None:
        if writer is None:
            return
        try:
            writer(event)
        except Exception as exc:  # noqa: BLE001 - parent disconnect does not strand the task
            self._append(
                task,
                {"type": "subagent_parent_event_error", "error": f"{type(exc).__name__}: {exc}"},
            )

    def _persist(self, task: SubagentTask) -> None:
        path = self.root / task.subagent_run_id / "task.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            **self._snapshot(task),
            "instruction": task.instruction,
            "event_sequence": task.event_sequence,
        }
        temporary = path.with_name(f"task.{uuid4().hex}.tmp")
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, default=str, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, run_id: str, *, wait_seconds: float = 0.0) -> dict[str, Any] | None:
        if not re.fullmatch(r"subagent-[0-9a-f]{12}", run_id):
            return None
        with self._lock:
            task = self._tasks.get(run_id)
        if task is None:
            return self._load_persisted(run_id)
        if wait_seconds > 0:
            with task.condition:
                task.condition.wait_for(
                    lambda: task.status in TERMINAL, timeout=min(30.0, max(0.0, wait_seconds))
                )
        return self._snapshot(task)

    def _load_persisted(self, run_id: str) -> dict[str, Any] | None:
        try:
            payload = json.loads((self.root / run_id / "task.json").read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            if payload.get("status") in {"queued", "running"}:
                payload.update(
                    status="interrupted",
                    error="daemon exited before this background task completed",
                )
            return {
                key: value
                for key, value in payload.items()
                if key not in {"instruction", "event_sequence"}
            }
        except (OSError, ValueError):
            return None

    def wait(self, run_id: str, timeout_seconds: float = 30.0) -> dict[str, Any] | None:
        return self.get(run_id, wait_seconds=timeout_seconds)

    def _snapshot(self, task: SubagentTask) -> dict[str, Any]:
        with task.condition:
            result = {
                **self._ids(task),
                "agent": task.agent,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "cancel_requested": task.cancel_requested,
            }
            if task.persistence_error:
                result["persistence_error"] = task.persistence_error
            return result

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(run_id)
            if task is None:
                saved = self.get(run_id)
                if saved and saved.get("status") in TERMINAL:
                    return {"ok": True, **saved}
                return {
                    "ok": False,
                    "error_type": "unknown_subagent_run",
                    "message": f"unknown Subagent run: {run_id}",
                }
            if task.status in TERMINAL:
                return {"ok": True, **self._snapshot(task)}
            task.cancel_requested = True
            task.cancel_event.set()
            if task.status == "queued":
                self._pending.pop(run_id, None)
                _, writer = self._runners.pop(run_id)
                self._finish(task, "cancelled", writer=writer)
            else:
                self._append(task, {"type": "subagent_cancel_requested", **self._ids(task)})
                try:
                    self._persist(task)
                except OSError as exc:
                    task.persistence_error = f"persistence_failed: {type(exc).__name__}"
                    return {"ok": False, "error_type": "persistence_failed", **self._snapshot(task)}
            snapshot = self._snapshot(task)
            return {"ok": not bool(task.persistence_error), **snapshot}

    def list_for_parent(self, parent_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            results = [
                self._snapshot(t) for t in self._tasks.values() if t.parent_run_id == parent_run_id
            ]
        known = {item["subagent_run_id"] for item in results}
        for path in self.root.glob("subagent-*/task.json"):
            run_id = path.parent.name
            payload = self._load_persisted(run_id)
            if payload and payload.get("parent_run_id") == parent_run_id and run_id not in known:
                results.append(payload)
        return results

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        if not re.fullmatch(r"subagent-[0-9a-f]{12}", run_id):
            return []
        with self._lock:
            task = self._tasks.get(run_id)
            if task is not None:
                with task.condition:
                    if task.events and after_seq >= task.events[0]["seq"] - 1:
                        return [item for item in task.events if item["seq"] > after_seq]
            results = []
            try:
                with (self.root / run_id / "events.jsonl").open(encoding="utf-8") as handle:
                    for line in handle:
                        try:
                            item = json.loads(line)
                            if int(item.get("seq", 0)) > after_seq:
                                results.append(item)
                        except (ValueError, TypeError):
                            continue
            except OSError:
                pass
            return results

    def close(self) -> None:
        with self._condition:
            self._accepting = False
            if not self._started:
                self._stopping = True
                return
            deadline = time.monotonic() + self.shutdown_grace_seconds
            while (self._pending or self._running) and time.monotonic() < deadline:
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))
            self._stopping = True
            for run_id in list(self._pending) + list(self._running):
                task = self._tasks.get(run_id)
                if task is not None and task.status not in TERMINAL:
                    task.cancel_event.set()
                    pair = self._runners.pop(run_id, None)
                    self._pending.pop(run_id, None)
                    self._finish(
                        task,
                        "interrupted",
                        error="daemon shutdown interrupted this task",
                        writer=pair[1] if pair else None,
                    )
            self._condition.notify_all()
        for thread in self._threads:
            thread.join(timeout=0.2)
