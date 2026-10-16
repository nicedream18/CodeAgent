from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.checkpoint import workspace_manifest

SESSION_ROOT = Path(".mokioclaw") / "session"
SESSION_FILE = "session.json"
SESSION_SUMMARY_FILE = "SESSION_SUMMARY.md"
MAX_RECENT_TURNS = 18
MAX_TURN_CONTENT = 1800
MAX_SESSION_SUMMARY = 5000
MAX_SESSION_CONTEXT = 7000


def session_dir(workspace: Path) -> Path:
    return workspace / SESSION_ROOT


def session_file(workspace: Path) -> Path:
    return session_dir(workspace) / SESSION_FILE


def session_summary_file(workspace: Path) -> Path:
    return workspace / SESSION_SUMMARY_FILE


def load_or_create_session(workspace: Path) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    path = session_file(workspace)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
    else:
        raw = {}
    session = _normalize_session(raw, workspace)
    save_session(workspace, session)
    return session


def append_user_turn(session: dict[str, Any], content: str) -> int:
    turn = int(session.get("turn_index", 0)) + 1
    session["turn_index"] = turn
    session["last_task"] = content
    session.setdefault("user_requirements", []).append(
        {"turn": turn, "content": content}
    )
    _append_turn(
        session,
        {
            "turn": turn,
            "role": "user",
            "route": "",
            "content": trim_text(content, MAX_TURN_CONTENT),
            "timestamp": utc_now(),
        },
    )
    return turn


def append_assistant_turn(
    session: dict[str, Any],
    *,
    turn: int,
    route: str,
    content: str,
    summary: str = "",
) -> None:
    session["last_route"] = route
    session["last_final_answer"] = trim_text(content, MAX_TURN_CONTENT)
    _append_turn(
        session,
        {
            "turn": turn,
            "role": "assistant",
            "route": route,
            "content": trim_text(content, MAX_TURN_CONTENT),
            "summary": trim_text(summary or content, 700),
            "timestamp": utc_now(),
        },
    )


def save_session(workspace: Path, session: dict[str, Any]) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    session = _normalize_session(session, workspace)
    _compact_session(session)
    session["updated_at"] = utc_now()
    path = session_file(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(session, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    session_summary_file(workspace).write_text(
        build_session_summary_markdown(workspace, session), encoding="utf-8"
    )
    return session


def compact_session_history(workspace: Path, *, keep_recent: int = 4) -> dict[str, Any]:
    from mokioclaw.core.session_lock import session_writer

    with session_writer(workspace):
        return _compact_saved_session(workspace, keep_recent=keep_recent)


def _compact_saved_session(workspace: Path, *, keep_recent: int = 4) -> dict[str, Any]:
    if keep_recent < 1:
        raise ValueError("keep_recent must be at least 1")
    session = load_or_create_session(workspace)
    turns = list(session.get("recent_turns", []))
    from langchain_core.messages import HumanMessage

    from mokioclaw.core.checkpoint import read_checkpoint
    from mokioclaw.core.context_budget import estimate_messages
    from mokioclaw.core.context_compaction import _atomic_json, critical_facts

    before = estimate_messages(
        None, [HumanMessage(content=build_session_context(workspace, session))]
    )[0]
    checkpoint = read_checkpoint(workspace)
    fact_state = {**checkpoint.get("summary", {}), "task": session.get("last_task", "")}
    try:
        catalogue = json.loads(
            (workspace / ".mokioclaw/checkpoints/context-facts.json").read_text(
                encoding="utf-8"
            )
        )
        fact_state.update(
            {key: value for key, value in catalogue.items() if key != "task"}
        )
    except (OSError, ValueError):
        catalogue = {}
    facts = critical_facts(fact_state)
    facts["user_requirements"] = session.get("user_requirements") or [
        {"turn": turn.get("turn"), "content": turn.get("content", "")}
        for turn in turns
        if turn.get("role") == "user"
    ]
    session["compaction_facts"] = facts
    session["context_version"] = int(session.get("context_version", 0)) + 1
    facts_file = session_dir(workspace) / f"facts-{session['context_version']}.json"
    _atomic_json(facts_file, facts)
    session["compaction_facts"]["full_facts_path"] = facts_file.relative_to(
        workspace
    ).as_posix()
    if len(turns) <= keep_recent:
        return {
            "ok": True,
            "compacted": 0,
            "kept": len(turns),
            "summary": session.get("summary", ""),
            "scope": "session",
            "status": "noop",
            "before_tokens": before,
            "after_tokens": before,
            "estimated": True,
            "estimation_method": "utf8_bytes",
        }
    older, kept = turns[:-keep_recent], turns[-keep_recent:]
    additions = []
    for turn in older:
        role = turn.get("role", "")
        route = f"/{turn.get('route')}" if turn.get("route") else ""
        summary = turn.get("summary") or turn.get("content", "")
        additions.append(
            f"turn {turn.get('turn')}: {role}{route}: {_head_tail(str(summary), 520)}"
        )
    session["summary"] = trim_text(
        "\n".join(
            part for part in [str(session.get("summary", "")), *additions] if part
        ),
        1800,
    )
    session["recent_turns"] = kept
    save_session(workspace, session)
    after = estimate_messages(
        None, [HumanMessage(content=build_session_context(workspace, session))]
    )[0]
    return {
        "ok": True,
        "compacted": len(older),
        "kept": len(kept),
        "summary": session.get("summary", ""),
        "scope": "session",
        "status": "applied",
        "before_tokens": before,
        "after_tokens": after,
        "estimated": True,
        "estimation_method": "utf8_bytes",
        "context_version": session["context_version"],
        "full_facts_path": facts_file.relative_to(workspace).as_posix(),
    }


def build_session_context(
    workspace: Path, session: dict[str, Any] | None = None
) -> str:
    session = _normalize_session(
        session or load_or_create_session(workspace), workspace
    )
    manifest = workspace_manifest(workspace, limit=40)
    files = [
        item.get("path", "")
        for item in manifest
        if item.get("type") == "file"
        and not str(item.get("path", "")).startswith(".mokioclaw/session/")
    ][:30]
    recent_turns = [
        {
            "turn": turn.get("turn"),
            "role": turn.get("role", ""),
            "route": turn.get("route", ""),
            "content": trim_text(str(turn.get("content", "")), 600),
            "summary": trim_text(str(turn.get("summary", "")), 300),
        }
        for turn in session.get("recent_turns", [])[-10:]
    ]
    payload = {
        "session_id": session.get("session_id", ""),
        "turn_index": session.get("turn_index", 0),
        "workspace": str(workspace),
        "summary": trim_text(str(session.get("summary", "")), 1800),
        "last_route": session.get("last_route", ""),
        "last_run_status": session.get("last_run_status", ""),
        "last_task": trim_text(str(session.get("last_task", "")), 600),
        "last_final_answer": trim_text(str(session.get("last_final_answer", "")), 900),
        "recent_turns": recent_turns,
        "recent_files": files,
        "compaction_facts": {
            key: value
            for key, value in session.get("compaction_facts", {}).items()
            if key not in {"task", "user_requirements"}
        },
        "user_requirements": session.get("user_requirements", []),
    }
    source_projection = payload["compaction_facts"].get("sources", [])
    if len(json.dumps(source_projection, ensure_ascii=False)) > 3000:
        payload["compaction_facts"]["sources"] = {
            "count": len(source_projection),
            "full_facts_path": payload["compaction_facts"].get("full_facts_path", ""),
        }
    # Required user corrections are never cut through a serialized JSON string.
    # The request guard will report if those requirements cannot fit the window.
    context_limit = (
        MAX_SESSION_CONTEXT // 2
        if session.get("context_version")
        else MAX_SESSION_CONTEXT
    )
    while (
        len(json.dumps(payload, ensure_ascii=False)) > context_limit
        and payload["recent_turns"]
    ):
        payload["recent_turns"].pop(0)
    if session.get("context_version"):
        while (
            len(json.dumps(payload, ensure_ascii=False)) > context_limit
            and payload["summary"]
        ):
            payload["summary"] = payload["summary"][: len(payload["summary"]) // 2]
        if len(json.dumps(payload, ensure_ascii=False)) > context_limit:
            payload["last_final_answer"] = ""
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def _head_tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit // 2] + "\n…\n" + text[-limit // 2 :]


def session_started_event(
    workspace: Path, session: dict[str, Any], *, resumed: bool = False
) -> dict[str, Any]:
    return {
        "type": "session_started",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn_index": session.get("turn_index", 0),
        "resumed": resumed,
        "session_file": str(session_file(workspace)),
        "summary_file": str(session_summary_file(workspace)),
    }


def session_turn_started_event(
    workspace: Path, session: dict[str, Any], *, turn: int, task: str
) -> dict[str, Any]:
    return {
        "type": "session_turn_started",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn": turn,
        "task": trim_text(task, 900),
    }


def session_turn_saved_event(
    workspace: Path, session: dict[str, Any], *, turn: int, route: str
) -> dict[str, Any]:
    return {
        "type": "session_turn_saved",
        "session_id": session.get("session_id", ""),
        "workspace": str(workspace),
        "turn": turn,
        "route": route,
        "turn_count": len(session.get("recent_turns", [])),
        "summary_file": str(session_summary_file(workspace)),
    }


def build_session_summary_markdown(workspace: Path, session: dict[str, Any]) -> str:
    lines = [
        "# MokioClaw Session Summary",
        "",
        f"- session_id: {session.get('session_id', '')}",
        f"- workspace: {workspace}",
        f"- turns: {session.get('turn_index', 0)}",
        f"- updated_at: {session.get('updated_at', '')}",
        f"- last_route: {session.get('last_route', '') or '(none)'}",
        "",
        "## Summary",
        "",
        str(session.get("summary", "") or "(no compressed summary yet)"),
        "",
        "## Recent Turns",
        "",
    ]
    for turn in session.get("recent_turns", [])[-MAX_RECENT_TURNS:]:
        role = turn.get("role", "")
        route = f" / {turn.get('route')}" if turn.get("route") else ""
        lines.append(
            f"- turn {turn.get('turn')}: {role}{route}: {trim_text(str(turn.get('content', '')), 260)}"
        )
    return "\n".join(lines).rstrip() + "\n"


def trim_text(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_session(raw: dict[str, Any], workspace: Path) -> dict[str, Any]:
    now = utc_now()
    session = dict(raw) if isinstance(raw, dict) else {}
    session.setdefault("version", 1)
    session.setdefault("session_id", f"session-{uuid4().hex[:8]}")
    session.setdefault("workspace", str(workspace))
    session.setdefault("created_at", now)
    session.setdefault("updated_at", now)
    session.setdefault("turn_index", 0)
    session.setdefault("summary", "")
    session.setdefault("recent_turns", [])
    session.setdefault("last_run_status", "")
    session.setdefault("last_route", "")
    session.setdefault("last_task", "")
    session.setdefault("last_final_answer", "")
    if not isinstance(session.get("recent_turns"), list):
        session["recent_turns"] = []
    session["workspace"] = str(workspace)
    session["turn_index"] = int(session.get("turn_index") or 0)
    session["summary"] = trim_text(str(session.get("summary", "")), MAX_SESSION_SUMMARY)
    return session


def _append_turn(session: dict[str, Any], turn: dict[str, Any]) -> None:
    turns = list(session.get("recent_turns", []))
    turns.append(turn)
    session["recent_turns"] = turns


def _compact_session(session: dict[str, Any]) -> None:
    turns = list(session.get("recent_turns", []))
    if len(turns) <= MAX_RECENT_TURNS:
        return
    older = turns[:-MAX_RECENT_TURNS]
    kept = turns[-MAX_RECENT_TURNS:]
    additions = []
    for turn in older:
        role = turn.get("role", "")
        route = f"/{turn.get('route')}" if turn.get("route") else ""
        summary = turn.get("summary") or turn.get("content", "")
        additions.append(
            f"turn {turn.get('turn')}: {role}{route}: {trim_text(str(summary), 260)}"
        )
    existing = str(session.get("summary", ""))
    session["summary"] = trim_text(
        "\n".join(part for part in [existing, *additions] if part), MAX_SESSION_SUMMARY
    )
    session["recent_turns"] = kept
