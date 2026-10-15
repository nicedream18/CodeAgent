from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator

from dotenv import load_dotenv
from langgraph.graph import add_messages

from mokioclaw.core.checkpoint import (
    CheckpointManager,
    load_resume_inputs,
    normalize_checkpoint_mode,
)
from mokioclaw.core.paths import default_workspace
from mokioclaw.core.session import (
    append_assistant_turn,
    append_user_turn,
    build_session_context,
    load_or_create_session,
    save_session,
    session_started_event,
    session_turn_saved_event,
    session_turn_started_event,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder, normalize_trace_mode
from mokioclaw.core.tool_policy import parse_bash_patterns, parse_tool_policies
from mokioclaw.agents.registry import parse_subagent_configs, registered_subagents
from mokioclaw.tools.mcp_tools import parse_mcp_servers
from mokioclaw.graph.workflow import build_complex_workflow, build_entry_workflow


def create_runtime(
    workspace: Path | None = None,
    *,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_from: Path | None = None,
    trace_mode: str | None = None,
) -> RuntimeState:
    load_dotenv()
    selected = workspace or resume_from or default_workspace()
    selected.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(
        workspace=selected,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int(
            "MOKIO_BASH_DEFAULT_TIMEOUT_SECONDS", 120
        ),
        bash_max_timeout_seconds=_env_int("MOKIO_BASH_MAX_TIMEOUT_SECONDS", 600),
        bash_max_output_chars=_env_int("MOKIO_BASH_MAX_OUTPUT_CHARS", 6000),
        bash_env_file=_env_path("MOKIO_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(
            checkpoint_mode or os.getenv("MOKIO_CHECKPOINT_MODE", "light")
        ),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(
            trace_mode or os.getenv("MOKIO_TRACE_MODE", "on")
        ),
        tool_policies=parse_tool_policies(os.getenv("MOKIO_TOOL_POLICIES")),
        bash_deny_patterns=parse_bash_patterns(
            os.getenv("MOKIO_BASH_DENY_PATTERNS"), variable="MOKIO_BASH_DENY_PATTERNS"
        ),
        bash_allow_patterns=parse_bash_patterns(
            os.getenv("MOKIO_BASH_ALLOW_PATTERNS"), variable="MOKIO_BASH_ALLOW_PATTERNS"
        ),
        tool_retry_limit=_env_int("MOKIO_TOOL_RETRY_LIMIT", 2),
        context_output_limit_chars=_env_int("MOKIO_CONTEXT_OUTPUT_LIMIT_CHARS", 12000),
        model_context_reserve_tokens=_env_int(
            "MOKIO_MODEL_CONTEXT_RESERVE_TOKENS", 8192
        ),
        subagent_configs=parse_subagent_configs(os.getenv("MOKIO_SUBAGENTS")),
        subagent_registry=registered_subagents(),
        mcp_servers=parse_mcp_servers(os.getenv("MOKIO_MCP_SERVERS")),
        mcp_timeout_seconds=_env_int("MOKIO_MCP_TIMEOUT_SECONDS", 30),
    )
    from mokioclaw.core.subagent_tasks import current_daemon_context

    daemon_context = current_daemon_context()
    if daemon_context is not None:
        (
            runtime.subagent_task_manager,
            runtime.parent_run_id,
            runtime.subagent_parent_writer,
        ) = daemon_context
    return runtime


def stream_agent_events(
    task: str | None = None,
    *,
    workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
) -> Iterator[dict[str, Any]]:
    if (use_daemon or os.getenv("MOKIO_USE_DAEMON") == "1") and os.getenv(
        "MOKIO_DAEMON_WORKER"
    ) != "1":
        from mokioclaw.core.daemon_client import run_via_daemon

        yield from run_via_daemon(
            "run",
            task,
            options={
                "workspace": workspace,
                "max_attempts": max_attempts,
                "approval_mode": approval_mode,
                "checkpoint_mode": checkpoint_mode,
                "resume_workspace": resume_workspace,
                "trace_mode": trace_mode,
            },
            approval_handler=approval_handler,
        )
        return
    resume_path = (
        resume_workspace.expanduser() if resume_workspace is not None else None
    )
    if resume_path is None:
        route = "workflow"
        entry_state: dict[str, Any] = {"task": task or "", "messages": []}
        for mode, event in build_entry_workflow().stream(
            entry_state, stream_mode=["updates", "custom"]
        ):
            if mode == "custom":
                yield {"type": "custom_event", "event": event}
                if isinstance(event, dict) and event.get("type") == "intent_decision":
                    route = str(event.get("route") or "workflow")
            else:
                _merge_graph_update(entry_state, event)
                yield {"type": "graph_event", "event": event}
        if route == "chat":
            return

    selected_workspace = resume_path or workspace
    state = create_runtime(
        selected_workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
    )
    workflow = build_complex_workflow()
    yield {"type": "workspace", "path": str(state.workspace)}

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(
            state, task=task, max_attempts=max_attempts
        )
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(
        current_state, status="started", latest_node="start"
    )
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(
                        current_state,
                        status="running",
                        latest_node=latest_node,
                        event={"mode": mode, "payload": event},
                    )
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(
                    current_state,
                    status="running",
                    latest_node=latest_node,
                    event={"mode": mode, "payload": event},
                )
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        saved = manager.save(
            current_state, status="interrupted", latest_node=latest_node
        )
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(
            status="interrupted", latest_node=latest_node, final_state=current_state
        )
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(
        status="finished", latest_node=latest_node, final_state=current_state
    )
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


def stream_session_events(
    task: str | None = None,
    *,
    session_workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str = "inline",
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
) -> Iterator[dict[str, Any]]:
    if (use_daemon or os.getenv("MOKIO_USE_DAEMON") == "1") and os.getenv(
        "MOKIO_DAEMON_WORKER"
    ) != "1":
        from mokioclaw.core.daemon_client import run_via_daemon

        yield from run_via_daemon(
            "session",
            task,
            options={
                "session_workspace": session_workspace,
                "max_attempts": max_attempts,
                "approval_mode": approval_mode,
                "checkpoint_mode": checkpoint_mode,
                "resume_workspace": resume_workspace,
                "trace_mode": trace_mode,
            },
            approval_handler=approval_handler,
        )
        return
    workspace = (
        resume_workspace or session_workspace or default_workspace()
    ).expanduser()
    workspace.mkdir(parents=True, exist_ok=True)
    session = load_or_create_session(workspace)
    resumed = resume_workspace is not None
    yield {
        "type": "custom_event",
        "event": session_started_event(workspace, session, resumed=resumed),
    }
    yield {"type": "workspace", "path": str(workspace)}

    if not task:
        return

    turn = append_user_turn(session, task)
    save_session(workspace, session)
    yield {
        "type": "custom_event",
        "event": session_turn_started_event(workspace, session, turn=turn, task=task),
    }
    session_context = build_session_context(workspace, session)

    route = "workflow"
    entry_state: dict[str, Any] = {
        "task": task or "",
        "messages": [],
        "session_id": session.get("session_id", ""),
        "session_turn": turn,
        "session_context": session_context,
    }
    for mode, event in build_entry_workflow().stream(
        entry_state, stream_mode=["updates", "custom"]
    ):
        if mode == "custom":
            yield {"type": "custom_event", "event": event}
            if isinstance(event, dict) and event.get("type") == "intent_decision":
                route = str(event.get("route") or "workflow")
        else:
            _merge_graph_update(entry_state, event)
            yield {"type": "graph_event", "event": event}

    if route == "chat":
        response = str(
            entry_state.get("chat_response") or entry_state.get("final_answer") or ""
        )
        append_assistant_turn(
            session, turn=turn, route="chat", content=response, summary=response
        )
        save_session(workspace, session)
        yield {
            "type": "custom_event",
            "event": session_turn_saved_event(
                workspace, session, turn=turn, route="chat"
            ),
        }
        return

    workflow_events = _stream_complex_workflow(
        task=task,
        workspace=workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
        session=session,
        turn=turn,
        session_context=session_context,
    )
    final_answer = ""
    for event in workflow_events:
        final_answer = _final_answer_from_event(event) or final_answer
        yield event

    append_assistant_turn(
        session, turn=turn, route="workflow", content=final_answer, summary=final_answer
    )
    save_session(workspace, session)
    yield {
        "type": "custom_event",
        "event": session_turn_saved_event(
            workspace, session, turn=turn, route="workflow"
        ),
    }


def _stream_complex_workflow(
    *,
    task: str | None,
    workspace: Path,
    max_attempts: int,
    approval_mode: str,
    approval_handler,
    checkpoint_mode: str | None,
    resume_workspace: Path | None,
    trace_mode: str | None,
    session: dict[str, Any] | None = None,
    turn: int | None = None,
    session_context: str = "",
) -> Iterator[dict[str, Any]]:
    resume_path = (
        resume_workspace.expanduser() if resume_workspace is not None else None
    )
    state = create_runtime(
        workspace,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_from=resume_path,
        trace_mode=trace_mode,
    )
    workflow = build_complex_workflow()

    resumed = False
    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs, resume_event = load_resume_inputs(
            state, task=task, max_attempts=max_attempts
        )
        resumed = True
        yield {"type": "custom_event", "event": resume_event}
    else:
        inputs = {
            "task": task or "",
            "runtime": state,
            "messages": [],
            "attempts": 0,
            "max_attempts": max_attempts,
        }

    if session is not None:
        inputs["session_id"] = session.get("session_id", "")
    if turn is not None:
        inputs["session_turn"] = turn
    if session_context:
        inputs["session_context"] = session_context
    metadata = dict(inputs.get("metadata", {}))
    if session is not None:
        metadata["session_id"] = session.get("session_id", "")
    if turn is not None:
        metadata["session_turn"] = turn
    if metadata:
        inputs["metadata"] = metadata

    current_state: dict[str, Any] = dict(inputs)
    manager = CheckpointManager(state, task=str(current_state.get("task", "")))
    trace = TraceRecorder(state, task=str(current_state.get("task", "")))
    trace.start(current_state, resumed=resumed, resume_event=resume_event)
    if resume_event is not None:
        trace.record_custom_event(resume_event)
    started_checkpoint = manager.save(
        current_state, status="started", latest_node="start"
    )
    if started_checkpoint:
        trace.record_custom_event(started_checkpoint)
    latest_node = "start"

    try:
        for mode, event in workflow.stream(inputs, stream_mode=["updates", "custom"]):
            if mode == "custom":
                trace.record_custom_event(event)
                if _custom_event_needs_checkpoint(event):
                    saved = manager.save(
                        current_state,
                        status="running",
                        latest_node=latest_node,
                        event={"mode": mode, "payload": event},
                    )
                    if saved:
                        trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": event}
            else:
                latest_node = _latest_graph_node(event) or latest_node
                _merge_graph_update(current_state, event)
                trace.record_graph_update(event)
                saved = manager.save(
                    current_state,
                    status="running",
                    latest_node=latest_node,
                    event={"mode": mode, "payload": event},
                )
                if saved:
                    trace.record_custom_event(saved)
                yield {"type": "graph_event", "event": event}
    except KeyboardInterrupt:
        saved = manager.save(
            current_state, status="interrupted", latest_node=latest_node
        )
        if saved:
            trace.record_custom_event(saved)
            yield {"type": "custom_event", "event": saved}
        trace_event = trace.end(
            status="interrupted", latest_node=latest_node, final_state=current_state
        )
        if trace_event:
            yield {"type": "custom_event", "event": trace_event}
        return

    saved = manager.save(current_state, status="finished", latest_node=latest_node)
    if saved:
        trace.record_custom_event(saved)
        yield {"type": "custom_event", "event": saved}
    trace_event = trace.end(
        status="finished", latest_node=latest_node, final_state=current_state
    )
    if trace_event:
        yield {"type": "custom_event", "event": trace_event}


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _env_path(name: str) -> Path | None:
    raw = os.getenv(name, "").strip()
    return Path(raw).expanduser() if raw else None


def _latest_graph_node(event: Any) -> str | None:
    if isinstance(event, dict) and event:
        return str(next(reversed(event)))
    return None


def _merge_graph_update(state: dict[str, Any], event: Any) -> None:
    if not isinstance(event, dict):
        return
    for update in event.values():
        if not isinstance(update, dict):
            continue
        for key, value in update.items():
            if key == "messages":
                state["messages"] = list(add_messages(state.get("messages", []), value))
            else:
                state[key] = value


def _custom_event_needs_checkpoint(event: Any) -> bool:
    if not isinstance(event, dict):
        return False
    if event.get("type") != "tool_result":
        return False
    result = event.get("result")
    if not isinstance(result, dict):
        return False
    return result.get("ok") is False or bool(result.get("requires_approval"))


def _final_answer_from_event(event: dict[str, Any]) -> str:
    if event.get("type") != "graph_event":
        return ""
    payload = event.get("event")
    if not isinstance(payload, dict):
        return ""
    update = payload.get("final")
    if not isinstance(update, dict):
        return ""
    return str(update.get("final_answer") or "")
