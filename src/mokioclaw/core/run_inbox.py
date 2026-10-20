"""Durable, idempotent foreground corrections and cooperative replanning."""

from __future__ import annotations

import copy
import json
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from mokioclaw.core.completion_budget import CompletionYield
from mokioclaw.core.loop_guard import LoopRecovery

_TOOL_PROGRESS = ContextVar("mokio_tool_progress", default=None)


def mark_tool_started():
    progress = _TOOL_PROGRESS.get()
    if progress is not None:
        progress["started"] = True


@contextmanager
def tool_call_batch(calls, messages, writer, node):
    """Close unfinished tool groups when a correction unwinds a nested agent."""
    from langchain_core.messages import ToolMessage

    progress = {"index": 0, "started": False}

    def pending():
        for index, call in enumerate(calls):
            progress.update(index=index, started=False)
            yield call

    token = _TOOL_PROGRESS.set(progress)
    try:
        yield pending()
    except (RunRedirected, CompletionYield, LoopRecovery) as signal:
        answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
        for offset, call in enumerate(calls[progress["index"] :]):
            if call["id"] in answered:
                continue
            result = {
                "ok": False,
                "status": "interrupted" if offset == 0 and progress["started"] else "not_executed",
                "error_type": ("collaboration_loop_detected" if isinstance(signal, LoopRecovery)
                               else "user_redirected" if isinstance(signal, RunRedirected)
                               else "completion_budget_reserved"),
                "error": "Current batch stopped; keep completed effects and reconsider unfinished work.",
            }
            messages.append(
                ToolMessage(
                    content=json.dumps(result),
                    name=call["name"],
                    tool_call_id=call["id"],
                )
            )
            writer(
                {
                    "type": "tool_result",
                    "node": node,
                    "name": call["name"],
                    "tool_call_id": call["id"],
                    "result": result,
                }
            )
        raise
    finally:
        _TOOL_PROGRESS.reset(token)


class RunRedirected(BaseException):
    """Control flow, deliberately outside ordinary model/tool error handlers."""

    def __init__(self):
        self.state = {}


class RunInbox:
    def __init__(self, path: Path | None = None, lock=None):
        self.path = path
        self.lock = lock or threading.RLock()
        self.items = []
        self.closed = False
        if path is not None and path.exists():
            self.items = json.loads(path.read_text(encoding="utf-8"))

    def _save(self):
        if self.path is not None:
            from mokioclaw.core.context_compaction import _atomic_json

            _atomic_json(self.path, self.items)

    def submit(self, message_id, text):
        if (
            not isinstance(message_id, str)
            or not 1 <= len(message_id) <= 128
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 8192
        ):
            return {"ok": False, "error_type": "invalid_input"}
        with self.lock:
            existing = next((m for m in self.items if m["message_id"] == message_id), None)
            if existing:
                if existing["text"] != text:
                    return {"ok": False, "error_type": "message_id_conflict"}
                return {"ok": True, **existing}
            if self.closed:
                return {"ok": False, "error_type": "run_finished"}
            if len(self.items) >= 100:
                return {"ok": False, "error_type": "inbox_full"}
            item = {
                "message_id": message_id,
                "text": text,
                "seq": len(self.items) + 1,
                "status": "queued",
            }
            self.items.append(item)
            try:
                self._save()
            except BaseException:
                # A retry must never acknowledge an item that failed durable receipt.
                self.items.pop()
                raise
            return {"ok": True, **item}

    def pending(self):
        with self.lock:
            return any(m["status"] == "queued" for m in self.items)

    def seal(self):
        with self.lock:
            if self.pending():
                return False
            self.closed = True
            return True

    def apply(self, state, writer=None):
        with self.lock:
            known = {
                m.get("message_id")
                for m in state.get("latest_user_corrections", [])
                if isinstance(m, dict)
            }
            corrections = list(state.get("latest_user_corrections", []))
            applied = []
            for item in self.items:
                if item["message_id"] not in known:
                    corrections.append({**item, "status": "applied"})
                    applied.append(item)
                item["status"] = "applied"
            self._save()
            state["latest_user_corrections"] = corrections
            state["messages"] = []
            state["passed"] = False
            state["final_answer"] = ""
            state["context_next_node"] = "planner"
            state["runtime"].context_version += 1
            if writer:
                for item in applied:
                    writer({"type": "user_message_applied", **item, "status": "applied"})
            runtime = state["runtime"]
            if (runtime.workspace / ".mokioclaw/session/session.json").exists():
                from mokioclaw.core.session import load_or_create_session, save_session

                session = load_or_create_session(runtime.workspace)
                merge_session_requirements(session, self)
                save_session(runtime.workspace, session)
            return applied


def merge_session_requirements(session, inbox):
    requirements = session.setdefault("user_requirements", [])
    known = {m.get("message_id") for m in requirements if isinstance(m, dict)}
    for item in inbox.items:
        if item["status"] == "applied" and item["message_id"] not in known:
            requirements.append(
                {
                    "turn": session.get("turn_index", 0),
                    "message_id": item["message_id"],
                    "content": item["text"],
                }
            )
            known.add(item["message_id"])


def check_redirect(runtime: Any, *, preserve_progress: bool = True) -> None:
    inbox = getattr(getattr(runtime, "run_control", None), "inbox", None)
    if inbox is not None and inbox.pending():
        redirect = RunRedirected()
        if preserve_progress:
            redirect.state = {
                key: copy.deepcopy(value)
                for key, value in (getattr(runtime, "foreground_state", None) or {}).items()
                if key not in {"runtime", "messages"}
            }
        raise redirect
