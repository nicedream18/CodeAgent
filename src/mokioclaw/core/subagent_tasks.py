from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
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
    events: list[dict[str, Any]] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition)
    cancel_event: threading.Event = field(default_factory=threading.Event)


class SubagentTaskManager:
    """Daemon-owned async queue with synchronous Runner execution in its own pool."""

    def __init__(
        self,
        runs_dir: Path,
        *,
        max_running: int = 4,
        max_queued: int = 16,
        shutdown_grace_seconds: float = 5.0,
    ) -> None:
        self.root = runs_dir
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_running = max_running
        self.max_queued = max_queued
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._lock = threading.RLock()
        self._tasks: dict[str, SubagentTask] = {}
        self._runners: dict[str, tuple[Callable[..., dict[str, Any]], Any]] = {}
        self._accepting = True
        self._mark_orphaned_tasks()
        self._ready = threading.Event()
        self._loop = asyncio.new_event_loop()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._executor = ThreadPoolExecutor(
            max_workers=max_running, thread_name_prefix="mokioclaw-subagent"
        )
        self._thread = threading.Thread(
            target=self._run_loop, name="mokioclaw-subagent-manager", daemon=True
        )
        self._started = False
        self._start_lock = threading.Lock()

    def start(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
            self._thread.start()
            asyncio.run_coroutine_threadsafe(self._start_workers(), self._loop)

    def _mark_orphaned_tasks(self) -> None:
        for path in self.root.glob("*/task.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("status") not in {"queued", "running"}:
                continue
            payload["status"] = "interrupted"
            payload["error"] = "daemon exited before this background task completed"
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
            event_path = path.parent / "events.jsonl"
            try:
                events = [
                    json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()
                ]
            except (OSError, json.JSONDecodeError):
                events = []
            event = {
                "type": "subagent_task_interrupted",
                "subagent_run_id": payload.get("subagent_run_id"),
                "parent_run_id": payload.get("parent_run_id"),
                "handoff_id": payload.get("handoff_id"),
                "status": "interrupted",
                "error": payload["error"],
            }
            item = {"seq": len(events) + 1, "event": event}
            with event_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False) + "\n")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def _start_workers(self) -> None:
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.max_running)]
        self._ready.set()

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
        self._ready.wait(timeout=5)
        with self._lock:
            if not self._accepting:
                return {
                    "ok": False,
                    "error_type": "daemon_stopping",
                    "message": "daemon is stopping",
                }
            queued = sum(task.status == "queued" for task in self._tasks.values())
            if queued >= self.max_queued:
                return {
                    "ok": False,
                    "error_type": "subagent_queue_full",
                    "message": "Subagent queue is full",
                }
            run_id = f"subagent-{uuid4().hex[:12]}"
            task = SubagentTask(run_id, parent_run_id, handoff_id, agent, instruction)
            self._tasks[run_id] = task
            self._persist(task)
        self._append(task, {"type": "subagent_task_queued", **self._ids(task)})
        self._notify_parent(
            task,
            parent_writer,
            {"type": "subagent_task_queued", **self._ids(task), "agent": agent},
        )
        # Keep runner closures in an in-memory dispatch table; they are daemon-local.
        with self._lock:
            self._runners[run_id] = (runner, parent_writer)
        asyncio.run_coroutine_threadsafe(self._queue.put(run_id), self._loop)
        return {
            "ok": True,
            "subagent_run_id": run_id,
            "status": "queued",
            **self._ids(task),
        }

    async def _worker(self) -> None:
        while True:
            run_id = await self._queue.get()
            if run_id == "__stop__":
                self._queue.task_done()
                return
            with self._lock:
                task = self._tasks.get(run_id)
                pair = self._runners.pop(run_id, None)
            if task is None or pair is None:
                self._queue.task_done()
                continue
            runner, parent_writer = pair
            if task.cancel_requested:
                self._set_status(task, "cancelled")
                self._append(task, {"type": "subagent_task_cancelled", **self._ids(task)})
                self._queue.task_done()
                continue
            self._set_status(task, "running")
            started = {
                "type": "subagent_task_started",
                **self._ids(task),
                "agent": task.agent,
            }
            self._append(task, started)
            self._notify_parent(task, parent_writer, started)

            def child_writer(event: dict[str, Any], task: SubagentTask = task) -> None:
                self._append(task, event)

            try:
                result = await self._loop.run_in_executor(
                    self._executor, runner, child_writer, task.cancel_event
                )
                task.result = result
                if task.cancel_requested:
                    self._set_status(task, "cancelled")
                    final_type = "subagent_task_cancelled"
                else:
                    self._set_status(task, "completed" if result.get("ok") else "failed")
                    final_type = (
                        "subagent_task_completed"
                        if task.status == "completed"
                        else "subagent_task_failed"
                    )
                summary = {
                    "type": final_type,
                    **self._ids(task),
                    "status": task.status,
                    "result": result,
                }
                self._append(task, summary)
                self._notify_parent(
                    task,
                    parent_writer,
                    {
                        "type": final_type,
                        **self._ids(task),
                        "status": task.status,
                        "ok": bool(result.get("ok")),
                        "summary": result.get("summary", ""),
                    },
                )
            except asyncio.CancelledError:
                if task.status not in TERMINAL:
                    task.error = "daemon shutdown interrupted this task"
                    self._set_status(task, "interrupted")
                raise
            except Exception as exc:  # noqa: BLE001 - runner failures become task state
                task.error = f"{type(exc).__name__}: {exc}"
                self._set_status(task, "failed")
                event = {
                    "type": "subagent_task_failed",
                    **self._ids(task),
                    "status": task.status,
                    "error": task.error,
                }
                self._append(task, event)
                self._notify_parent(task, parent_writer, event)
            finally:
                self._queue.task_done()

    async def _stop_workers(self) -> None:
        for _ in range(self.max_running):
            await self._queue.put("__stop__")
        await self._queue.join()
        await asyncio.gather(*self._workers, return_exceptions=True)

    async def _cancel_workers(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    @staticmethod
    def _ids(task: SubagentTask) -> dict[str, str]:
        return {
            "subagent_run_id": task.subagent_run_id,
            "parent_run_id": task.parent_run_id,
            "handoff_id": task.handoff_id,
        }

    def _set_status(self, task: SubagentTask, status: str) -> None:
        with task.condition:
            task.status = status
            task.condition.notify_all()
        self._persist(task)

    def _append(self, task: SubagentTask, event: dict[str, Any]) -> int:
        with task.condition:
            item = {"seq": len(task.events) + 1, "event": event}
            task.events.append(item)
            path = self.root / task.subagent_run_id / "events.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            task.condition.notify_all()
            return int(item["seq"])

    def _notify_parent(
        self,
        task: SubagentTask,
        writer: Callable[[dict[str, Any]], Any] | None,
        event: dict[str, Any],
    ) -> None:
        if writer is None:
            return
        try:
            writer(event)
        except Exception as exc:  # noqa: BLE001 - parent event failures must not strand work
            self._append(
                task,
                {
                    "type": "subagent_parent_event_error",
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )

    def _persist(self, task: SubagentTask) -> None:
        path = self.root / task.subagent_run_id / "task.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with task.condition:
            payload = {
                **self._ids(task),
                "agent": task.agent,
                "instruction": task.instruction,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "cancel_requested": task.cancel_requested,
            }
            temporary = path.with_name(f"task.{uuid4().hex}.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, default=str, indent=2),
                encoding="utf-8",
            )
            temporary.replace(path)

    def get(self, run_id: str, *, wait_seconds: float = 0.0) -> dict[str, Any] | None:
        if not re.fullmatch(r"subagent-[0-9a-f]{12}", run_id):
            return None
        with self._lock:
            task = self._tasks.get(run_id)
        if task is None:
            return self._load_persisted(run_id)
        if wait_seconds > 0:
            with task.condition:
                if task.status not in TERMINAL:
                    task.condition.wait(timeout=min(30.0, max(0.0, wait_seconds)))
        return self._snapshot(task)

    def _load_persisted(self, run_id: str) -> dict[str, Any] | None:
        path = self.root / run_id / "task.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def wait(self, run_id: str, timeout_seconds: float = 30.0) -> dict[str, Any] | None:
        return self.get(run_id, wait_seconds=min(30.0, max(0.0, timeout_seconds)))

    def _snapshot(self, task: SubagentTask) -> dict[str, Any]:
        with task.condition:
            return {
                **self._ids(task),
                "agent": task.agent,
                "status": task.status,
                "result": task.result,
                "error": task.error,
                "cancel_requested": task.cancel_requested,
            }

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(run_id)
        if task is None:
            return {
                "ok": False,
                "error_type": "unknown_subagent_run",
                "message": f"unknown Subagent run: {run_id}",
            }
        with task.condition:
            if task.status in TERMINAL:
                return {"ok": True, **self._snapshot(task)}
            task.cancel_requested = True
            task.cancel_event.set()
            queued = task.status == "queued"
        if queued:
            self._set_status(task, "cancelled")
            event = {
                "type": "subagent_task_cancelled",
                **self._ids(task),
                "status": "cancelled",
            }
            self._append(task, event)
        else:
            self._append(task, {"type": "subagent_cancel_requested", **self._ids(task)})
        self._persist(task)
        return {"ok": True, **self._snapshot(task)}

    def list_for_parent(self, parent_run_id: str) -> list[dict[str, Any]]:
        with self._lock:
            tasks = list(self._tasks.values())
        results = [self._snapshot(task) for task in tasks if task.parent_run_id == parent_run_id]
        known = {item["subagent_run_id"] for item in results}
        for path in self.root.glob("subagent-*/task.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            run_id = str(payload.get("subagent_run_id", ""))
            if payload.get("parent_run_id") == parent_run_id and run_id not in known:
                results.append(payload)
        return results

    def events(self, run_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        if not re.fullmatch(r"subagent-[0-9a-f]{12}", run_id):
            return []
        with self._lock:
            task = self._tasks.get(run_id)
        if task is None:
            path = self.root / run_id / "events.jsonl"
            try:
                return [
                    item
                    for item in (
                        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                    )
                    if int(item.get("seq", 0)) > after_seq
                ]
            except (OSError, json.JSONDecodeError):
                return []
        with task.condition:
            return [item for item in task.events if int(item["seq"]) > after_seq]

    def close(self) -> None:
        with self._lock:
            self._accepting = False
        if not self._started:
            return
        shutdown_future = asyncio.run_coroutine_threadsafe(self._stop_workers(), self._loop)
        try:
            shutdown_future.result(timeout=self.shutdown_grace_seconds)
        except TimeoutError:
            shutdown_future.cancel()
            with self._lock:
                tasks = list(self._tasks.values())
            for task in tasks:
                if task.status in {"queued", "running"}:
                    task.cancel_event.set()
                    task.error = "daemon shutdown interrupted this task"
                    self._set_status(task, "interrupted")
                    self._append(
                        task,
                        {
                            "type": "subagent_task_interrupted",
                            **self._ids(task),
                            "status": "interrupted",
                            "error": task.error,
                        },
                    )
            try:
                asyncio.run_coroutine_threadsafe(self._cancel_workers(), self._loop).result(
                    timeout=1
                )
            except (TimeoutError, RuntimeError):
                self._loop.call_soon_threadsafe(self._loop.stop)
        self._executor.shutdown(wait=False, cancel_futures=True)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1)
