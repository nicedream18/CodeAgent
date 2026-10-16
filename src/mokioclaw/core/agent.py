from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv
from langgraph.graph import add_messages

from mokioclaw.agents.registry import parse_subagent_configs, registered_subagents
from mokioclaw.core.checkpoint import (
    CheckpointManager,
    load_resume_inputs,
    normalize_checkpoint_mode,
)
from mokioclaw.core.paths import default_workspace
from mokioclaw.core.run_control import (
    CURRENT_RUN,
    RunControl,
    RunInterrupted,
    check_run,
)
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
from mokioclaw.core.tool_policy import parse_bash_patterns, parse_tool_policies
from mokioclaw.core.trace import TraceRecorder, normalize_trace_mode
from mokioclaw.graph.workflow import build_complex_workflow, build_entry_workflow
from mokioclaw.tools.mcp_tools import parse_mcp_servers


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
        context_result_limit_chars=_env_int("MOKIO_CONTEXT_RESULT_LIMIT_CHARS", 24000),
        context_max_items=_env_int("MOKIO_CONTEXT_MAX_ITEMS", 100),
        context_max_depth=_env_int("MOKIO_CONTEXT_MAX_DEPTH", 8),
        context_max_nodes=_env_int("MOKIO_CONTEXT_MAX_NODES", 1000),
        model_context_reserve_tokens=int(
            os.getenv("MOKIO_MODEL_CONTEXT_RESERVE_TOKENS", "8192")
        ),
        context_safety_tokens=int(os.getenv("MOKIO_CONTEXT_SAFETY_TOKENS", "1024")),
        context_high_ratio=float(os.getenv("MOKIO_CONTEXT_HIGH_RATIO", "0.85")),
        context_target_ratio=float(os.getenv("MOKIO_CONTEXT_TARGET_RATIO", "0.60")),
        subagent_configs=parse_subagent_configs(os.getenv("MOKIO_SUBAGENTS")),
        subagent_registry=registered_subagents(),
        mcp_servers=parse_mcp_servers(os.getenv("MOKIO_MCP_SERVERS")),
        mcp_timeout_seconds=_env_int("MOKIO_MCP_TIMEOUT_SECONDS", 30),
    )
    from mokioclaw.core.context_budget import ContextBudget
    from mokioclaw.core.context_control import CURRENT_CONTROL, ContextControl

    ContextBudget.from_runtime(runtime)
    runtime.context_control = CURRENT_CONTROL.get() or ContextControl()
    runtime.run_control = CURRENT_RUN.get()
    from mokioclaw.core.subagent_tasks import current_daemon_context

    daemon_context = current_daemon_context()
    if daemon_context is not None:
        (
            runtime.subagent_task_manager,
            runtime.parent_run_id,
            runtime.subagent_parent_writer,
        ) = daemon_context
    return runtime


class _RunLifecycle:
    def __init__(self, runtime, task, max_attempts):
        self.runtime = runtime
        self.state = {
            "runtime": runtime,
            "task": task or "",
            "messages": [],
            "max_attempts": max_attempts,
        }
        self.latest_node = "start"
        self.resume_inputs = None
        self.resume_event = None
        self.trace = TraceRecorder(runtime, task=task or "")
        runtime.run_control.event_sink = self.record_emitted
        self.manager = CheckpointManager(runtime, task=task or "")

    def record_emitted(self, event):
        if event.get("type") == "llm_stream_start":
            self.latest_node = event.get("node") or self.latest_node
        self.trace.record_emitted_event(event)

    def record(self, envelope):
        event = envelope.get("event", {})
        if envelope.get("type") == "graph_event":
            self.latest_node = _latest_graph_node(event) or self.latest_node
            _merge_graph_update(self.state, event)
            self.trace.record_graph_update(event)
        elif envelope.get("type") == "custom_event":
            if event.get("type") == "llm_stream_start":
                self.latest_node = event.get("node") or self.latest_node
            self.trace.record_custom_event(event)
        if envelope.get("type") == "graph_event" or _custom_event_needs_checkpoint(
            event
        ):
            saved = self.manager.save(
                self.state, status="running", latest_node=self.latest_node
            )
            if saved:
                self.trace.record_custom_event(saved)


def _controlled_stream(
    stream, task, options, control, *, session, execution_control=None
):
    from contextlib import nullcontext

    from mokioclaw.core.context_control import CURRENT_CONTROL, ContextControl
    from mokioclaw.core.session_lock import session_writer

    remote = (
        options.get("use_daemon") or os.getenv("MOKIO_USE_DAEMON") == "1"
    ) and os.getenv("MOKIO_DAEMON_WORKER") != "1"
    if remote:
        yield from stream(task, **options)
        return
    selected = (
        options.get("resume_workspace")
        or options.get("session_workspace")
        or options.get("workspace")
        or default_workspace()
    ).expanduser()
    options["session_workspace" if session else "workspace"] = selected
    previous = CURRENT_CONTROL.get()
    active_control = control or previous or ContextControl()
    context_token = CURRENT_CONTROL.set(active_control)
    active_run = execution_control or CURRENT_RUN.get() or RunControl()
    run_token = CURRENT_RUN.set(active_run)
    lifecycle = None
    try:
        with session_writer(selected) if session else nullcontext():
            runtime = create_runtime(
                selected,
                approval_mode=options["approval_mode"],
                approval_handler=options["approval_handler"],
                checkpoint_mode=options["checkpoint_mode"],
                trace_mode=options["trace_mode"],
                resume_from=options["resume_workspace"],
            )
            lifecycle = _RunLifecycle(runtime, task, options["max_attempts"])
            if options["resume_workspace"]:
                # Read the previous checkpoint before writing this run's start snapshot.
                lifecycle.resume_inputs, lifecycle.resume_event = load_resume_inputs(
                    runtime, task=task, max_attempts=options["max_attempts"]
                )
                lifecycle.state.update(lifecycle.resume_inputs)
                restored_task = str(lifecycle.state.get("task", ""))
                lifecycle.trace.task = lifecycle.manager.task = restored_task
            lifecycle.trace.start(
                lifecycle.state,
                resumed=bool(options["resume_workspace"]),
                resume_event=lifecycle.resume_event,
            )
            initial = lifecycle.manager.save(
                lifecycle.state, status="started", latest_node="start"
            )
            if initial:
                lifecycle.trace.record_custom_event(initial)
            status, failure = "finished", None
            try:
                if runtime.parent_run_id is None:
                    started = {
                        "type": "custom_event",
                        "event": {
                            "type": "run_started",
                            "run_id": f"local-{uuid4().hex[:12]}",
                        },
                    }
                    lifecycle.record(started)
                    yield started
                active_run.check()
                for envelope in stream(task, **options, _lifecycle=lifecycle):
                    lifecycle.record(envelope)
                    yield envelope
                active_run.check()
            except (RunInterrupted, KeyboardInterrupt):
                active_run.cancel()
                status = "interrupted"
            except Exception as exc:
                status, failure = "failed", exc
            finally:
                # Model/MCP cleanup completes before the terminal status is published.
                active_run.close()
            status = active_run.finish(status)
            if session and (selected / ".mokioclaw/session/session.json").exists():
                saved_session = load_or_create_session(selected)
                saved_session["last_run_status"] = status
                save_session(selected, saved_session)
            saved = lifecycle.manager.save(
                lifecycle.state, status=status, latest_node=lifecycle.latest_node
            )
            if saved:
                lifecycle.trace.record_custom_event(saved)
                yield {"type": "custom_event", "event": saved}
            trace_event = lifecycle.trace.end(
                status=status,
                latest_node=lifecycle.latest_node,
                final_state=lifecycle.state,
            )
            if trace_event:
                yield {"type": "custom_event", "event": trace_event}
            yield {
                "type": "custom_event",
                "event": {"type": "run_finished", "status": status},
            }
            if failure is not None:
                raise failure
    finally:
        active_run.event_sink = None
        CURRENT_RUN.reset(run_token)
        CURRENT_CONTROL.reset(context_token)
        active_run.close()
        if previous is None:
            active_control.close()


def _graph_envelopes(workflow, inputs):
    check_run(inputs.get("runtime"))
    iterator = workflow.stream(inputs, stream_mode=["updates", "custom"])
    try:
        for mode, event in iterator:
            yield {
                "type": "custom_event" if mode == "custom" else "graph_event",
                "event": event,
            }
            check_run(inputs.get("runtime"))
    finally:
        if hasattr(iterator, "close"):
            iterator.close()


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
    context_control=None,
    run_control=None,
) -> Iterator[dict[str, Any]]:
    options = {
        "workspace": workspace,
        "max_attempts": max_attempts,
        "approval_mode": approval_mode,
        "approval_handler": approval_handler,
        "checkpoint_mode": checkpoint_mode,
        "resume_workspace": resume_workspace,
        "trace_mode": trace_mode,
        "use_daemon": use_daemon,
    }
    yield from _controlled_stream(
        _stream_agent_events,
        task,
        options,
        context_control,
        session=False,
        execution_control=run_control,
    )


def _stream_agent_events(
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
    _lifecycle=None,
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
    runtime = _lifecycle.runtime
    yield {"type": "workspace", "path": str(runtime.workspace)}
    if resume_workspace is None:
        route = "workflow"
        entry_state = {"runtime": runtime, "task": task or "", "messages": []}
        for envelope in _graph_envelopes(build_entry_workflow(), entry_state):
            event = envelope["event"]
            if (
                envelope["type"] == "custom_event"
                and event.get("type") == "intent_decision"
            ):
                route = str(event.get("route") or "workflow")
            elif envelope["type"] == "graph_event":
                _merge_graph_update(entry_state, event)
            yield envelope
        if route == "chat":
            return
    yield from _stream_complex_workflow(
        task=task,
        workspace=runtime.workspace,
        max_attempts=max_attempts,
        approval_mode=approval_mode,
        approval_handler=approval_handler,
        checkpoint_mode=checkpoint_mode,
        resume_workspace=resume_workspace,
        trace_mode=trace_mode,
        _lifecycle=_lifecycle,
    )


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
    context_control=None,
    run_control=None,
) -> Iterator[dict[str, Any]]:
    options = {
        "session_workspace": session_workspace,
        "max_attempts": max_attempts,
        "approval_mode": approval_mode,
        "approval_handler": approval_handler,
        "checkpoint_mode": checkpoint_mode,
        "resume_workspace": resume_workspace,
        "trace_mode": trace_mode,
        "use_daemon": use_daemon,
    }
    yield from _controlled_stream(
        _stream_session_events,
        task,
        options,
        context_control,
        session=True,
        execution_control=run_control,
    )


def _stream_session_events(
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
    _lifecycle=None,
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
        "runtime": _lifecycle.runtime,
        "task": task or "",
        "messages": [],
        "session_id": session.get("session_id", ""),
        "session_turn": turn,
        "session_context": session_context,
    }
    _lifecycle.state.update(entry_state)
    for envelope in _graph_envelopes(build_entry_workflow(), entry_state):
        event = envelope["event"]
        if (
            envelope["type"] == "custom_event"
            and event.get("type") == "intent_decision"
        ):
            route = str(event.get("route") or "workflow")
        elif envelope["type"] == "graph_event":
            _merge_graph_update(entry_state, event)
        yield envelope

    if route == "chat":
        response = str(
            entry_state.get("chat_response") or entry_state.get("final_answer") or ""
        )
        check_run(_lifecycle.runtime)
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
        _lifecycle=_lifecycle,
    )
    final_answer = ""
    for event in workflow_events:
        final_answer = _final_answer_from_event(event) or final_answer
        yield event

    check_run(_lifecycle.runtime)
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
    _lifecycle=None,
) -> Iterator[dict[str, Any]]:
    resume_path = (
        resume_workspace.expanduser() if resume_workspace is not None else None
    )
    state = _lifecycle.runtime
    workflow = build_complex_workflow()

    resume_event: dict[str, Any] | None = None
    if resume_path is not None:
        inputs = dict(_lifecycle.resume_inputs)
        resume_event = _lifecycle.resume_event
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
        import json

        inputs["session_context"] = session_context
        session_payload = json.loads(session_context)
        inputs["latest_user_corrections"] = session_payload.get("user_requirements", [])
    state.context_version = int(inputs.get("context_version", 0))
    metadata = dict(inputs.get("metadata", {}))
    if session is not None:
        metadata["session_id"] = session.get("session_id", "")
    if turn is not None:
        metadata["session_turn"] = turn
    if metadata:
        inputs["metadata"] = metadata

    _lifecycle.state.update(inputs)
    yield from _graph_envelopes(workflow, inputs)


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
