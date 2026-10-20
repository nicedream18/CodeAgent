# ruff: noqa: E402, F401
# Compatibility facade imports implementations after defining shared dependencies.
from __future__ import annotations

import os  # noqa: F401
from collections.abc import Iterator  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any  # noqa: F401
from uuid import uuid4  # noqa: F401

from dotenv import load_dotenv  # noqa: F401

from mokioclaw.agents.registry import parse_subagent_configs, registered_subagents  # noqa: F401
from mokioclaw.core.checkpoint import (
    CheckpointManager,
    load_resume_inputs,
    normalize_checkpoint_mode,
)  # noqa: F401
from mokioclaw.core.paths import default_workspace  # noqa: F401
from mokioclaw.core.run_control import (
    CURRENT_RUN,
    RunControl,
    RunInterrupted,
    check_run,
)  # noqa: F401
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
from mokioclaw.core.state import RuntimeState  # noqa: F401
from mokioclaw.core.tool_policy import parse_bash_patterns, parse_tool_policies  # noqa: F401
from mokioclaw.core.trace import TraceRecorder, normalize_trace_mode  # noqa: F401
from mokioclaw.graph.workflow import build_complex_workflow, build_entry_workflow  # noqa: F401
from mokioclaw.orchestration.messages import add_messages  # noqa: F401
from mokioclaw.providers.generation import ModelGenerationStopped  # noqa: F401
from mokioclaw.providers.model_policy import RunBudgetExceeded  # noqa: F401
from mokioclaw.tools.mcp_tools import parse_mcp_servers  # noqa: F401


def create_runtime(
    workspace: Path | None = None,
    *,
    approval_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_from: Path | None = None,
    trace_mode: str | None = None,
    execution_backend: str | None = None,
    workflow_mode: str | None = None,
    skill: str | None = None,
) -> RuntimeState:
    load_dotenv()
    selected = workspace or resume_from or default_workspace()
    selected = selected.expanduser().resolve()
    selected.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(
        workspace=selected,
        approval_mode=approval_mode or os.getenv("MOKIO_APPROVAL_MODE", "inline"),
        approval_handler=approval_handler,
        bash_default_timeout_seconds=_env_int("MOKIO_BASH_DEFAULT_TIMEOUT_SECONDS", 120),
        bash_max_timeout_seconds=_env_int("MOKIO_BASH_MAX_TIMEOUT_SECONDS", 600),
        bash_max_output_chars=_env_int("MOKIO_BASH_MAX_OUTPUT_CHARS", 6000),
        bash_env_file=_env_path("MOKIO_BASH_ENV_FILE"),
        checkpoint_mode=normalize_checkpoint_mode(
            checkpoint_mode or os.getenv("MOKIO_CHECKPOINT_MODE", "light")
        ),
        resume_from=resume_from,
        trace_mode=normalize_trace_mode(trace_mode or os.getenv("MOKIO_TRACE_MODE", "on")),
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
        model_context_reserve_tokens=int(os.getenv("MOKIO_MODEL_CONTEXT_RESERVE_TOKENS", "8192")),
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
    from mokioclaw.core.settings import configure_runtime

    configure_runtime(
        runtime,
        explicit={
            "execution_backend": execution_backend,
            "workflow_mode": workflow_mode,
            "approval_mode": approval_mode,
            "checkpoint_mode": checkpoint_mode,
            "trace_mode": trace_mode,
        },
    )

    runtime.explicit_skill = skill
    from mokioclaw.core.agent_profiles import load_agent_profiles

    load_agent_profiles(runtime)
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
    runtime.run_id = runtime.parent_run_id or f"local-{uuid4().hex}"
    if runtime.run_control is not None:
        runtime.run_control.run_id = runtime.run_id
        runtime.run_control.runtime = runtime
        with runtime.run_control.inbox.lock:
            runtime.run_control.inbox.path = (
                runtime.workspace / ".mokioclaw/inbox" / f"{runtime.run_id}.json"
            )
            runtime.run_control.inbox._save()
    initializer = getattr(runtime.run_control, "runtime_initializer", None)
    if initializer is not None:
        initializer(runtime)
    return runtime


def stream_agent_events(
    task: str | None = None,
    *,
    workspace: Path | None = None,
    max_attempts: int = 3,
    approval_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
    execution_backend: str | None = None,
    workflow_mode: str | None = None,
    skill: str | None = None,
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
        "execution_backend": execution_backend,
        "workflow_mode": workflow_mode,
        "skill": skill,
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
    approval_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
    execution_backend: str | None = None,
    workflow_mode: str | None = None,
    skill: str | None = None,
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
                "execution_backend": execution_backend,
                "workflow_mode": workflow_mode,
                "skill": skill,
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
            if envelope["type"] == "custom_event" and event.get("type") == "intent_decision":
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
    approval_mode: str | None = None,
    approval_handler=None,
    checkpoint_mode: str | None = None,
    resume_workspace: Path | None = None,
    trace_mode: str | None = None,
    use_daemon: bool = False,
    execution_backend: str | None = None,
    workflow_mode: str | None = None,
    skill: str | None = None,
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
        "execution_backend": execution_backend,
        "workflow_mode": workflow_mode,
        "skill": skill,
    }
    yield from _controlled_stream(
        _stream_session_events,
        task,
        options,
        context_control,
        session=True,
        execution_control=run_control,
    )


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
    if event.get("type") in {"loop_replan", "loop_stop"}:
        return True
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


# Public compatibility facade; implementation dependencies remain patchable here.
from mokioclaw.core.runner.events import _graph_envelopes as _graph_envelopes  # noqa: F401
from mokioclaw.core.runner.events import _stream_complex_workflow as _stream_complex_workflow  # noqa: F401
from mokioclaw.core.runner.lifecycle import _controlled_stream as _controlled_stream  # noqa: F401
from mokioclaw.core.runner.lifecycle import _RunLifecycle as _RunLifecycle  # noqa: F401
from mokioclaw.core.runner.session import _stream_session_events as _stream_session_events  # noqa: F401
