from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import replace
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from langgraph.config import get_stream_writer
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from mokioclaw.agents.code_agent import run_code_agent as _DEFAULT_CODE_AGENT
from mokioclaw.agents.registry import registered_subagents, run_subagent
from mokioclaw.agents.search_agent import run_search_agent as _DEFAULT_SEARCH_AGENT
from mokioclaw.core.run_control import RunInterrupted, check_run

# Kept as patchable compatibility hooks for callers/tests that replace the
# original specialist runners.
run_code_agent = _DEFAULT_CODE_AGENT
run_search_agent = _DEFAULT_SEARCH_AGENT
from mokioclaw.core.context_budget import ContextBudget
from mokioclaw.core.context_compaction import graph_context_updates
from mokioclaw.core.tool_output import bound_tool_result
from mokioclaw.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
)
from mokioclaw.graph.state import MokioGraphState, TodoItem, VerificationCheck
from mokioclaw.prompts.stage3 import PLANNER_PROMPT, VERIFIER_PROMPT
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools import build_read_only_tools
from mokioclaw.tools.execution import execute_tool, tool_failure_result
from mokioclaw.tools.registry import tool_spec_for
from mokioclaw.tools.todo_tool import persist_todos, write_todos

DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

AMIYA_TODOS = [
    "Research Amiya and collect reliable source links.",
    "Create amiya_profile.html with a polished character introduction.",
    "Include at least two source links in the HTML.",
    "Run non-interactive checks for the generated HTML file.",
]

AMIYA_CRITERIA = [
    "amiya_profile.html exists in the workspace.",
    "The page mentions 阿米娅 and 明日方舟.",
    "The page introduces identity, traits, abilities, and story role.",
    "The page includes at least two source links.",
]

AMIYA_COMMANDS = [
    "python -c \"from pathlib import Path; p=Path('amiya_profile.html'); s=p.read_text(encoding='utf-8'); assert '阿米娅' in s and '明日方舟' in s; assert s.lower().count('http') >= 2; print('amiya html ok')\"",
]

DEFAULT_TODOS = [
    "Clarify the deliverable and acceptance criteria.",
    "Delegate specialist work needed for the task.",
    "Verify the generated result.",
]

INTENT_ROUTER_PROMPT = """You are the intent router for MokioClaw.

Classify the user's latest input into exactly one route:

- chat: greetings, thanks, identity/help questions, ordinary conceptual Q&A, or conversational messages that do not need workspace access.
- workflow: any request that needs creating/editing/reading files, running commands, installing packages, searching the web, checking the current project, verifying a result, or producing a concrete deliverable.

When session context is provided, use it only to understand whether the latest
input is a continuation of prior coding work. A short follow-up like "继续",
"修一下", or "运行测试" should be workflow if it refers to prior workspace work.

Return only JSON with this shape:
{"route":"chat"|"workflow","reason":"brief reason","confidence":0.0}

If uncertain, choose workflow.
"""

CHAT_RESPONDER_PROMPT = """You are MokioClaw's lightweight chat node.

Answer the user directly and concisely. Do not claim that you read files,
searched the web, ran commands, edited files, or inspected the workspace.
If the user asks for work requiring tools or project context, say that it
should be handled by the workflow route.

If session context is provided, you may use the recent conversation summary to
answer conversational follow-ups, but do not invent workspace facts.
"""


def intent_router_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    route = "workflow"
    reason = "router fallback: default to workflow"
    confidence = 0.0
    try:
        response = invoke_streaming(
            create_model(),
            [
                SystemMessage(content=INTENT_ROUTER_PROMPT),
                HumanMessage(content=_router_input(state)),
            ],
            writer=writer,
            node="intent_router",
            state=state,
        )
        parsed = _extract_json(str(response.content)) or {}
        candidate = str(parsed.get("route", "")).strip().lower()
        parsed_confidence = _coerce_confidence(parsed.get("confidence"))
        if candidate in {"chat", "workflow"} and parsed_confidence >= 0.55:
            route = candidate
            confidence = parsed_confidence
            reason = str(parsed.get("reason") or "")
        else:
            reason = str(
                parsed.get("reason")
                or "router returned low-confidence or invalid route"
            )
            confidence = parsed_confidence
    except RunInterrupted:
        raise
    except Exception as exc:
        reason = f"router error: {type(exc).__name__}: {exc}"

    event = {
        "type": "intent_decision",
        "route": route,
        "reason": reason,
        "confidence": confidence,
    }
    writer(event)
    return {
        "intent_route": route,
        "intent_reason": reason,
        "intent_confidence": confidence,
    }


def intent_route_fn(state: MokioGraphState) -> str:
    return "chat_responder" if state.get("intent_route") == "chat" else "planner"


def chat_responder_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    try:
        response = invoke_streaming(
            create_model(),
            [
                SystemMessage(content=CHAT_RESPONDER_PROMPT),
                HumanMessage(content=_chat_input(state)),
            ],
            writer=writer,
            node="chat_responder",
            state=state,
        )
        text = str(getattr(response, "content", "") or "").strip()
    except RunInterrupted:
        raise
    except Exception as exc:
        text = f"这是轻量聊天分支，但模型回复暂不可用：{type(exc).__name__}: {exc}"
    if not text:
        text = "我在。你可以继续提问，或者直接描述一个需要我完成的任务。"
    event = {
        "type": "chat_response",
        "mode": "lightweight",
        "reason": state.get("intent_reason", ""),
        "response": text,
    }
    writer(event)
    return {"chat_response": text, "final_answer": text}


def planner_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    working_state: MokioGraphState = {**state}
    if not working_state.get("todos"):
        _apply_plan(working_state, _default_plan(working_state["task"]))
        persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    memory = build_layered_memory(working_state, node="planner")
    writer(memory_event(memory, node="planner"))
    model = create_model()
    planner = model.bind_tools(_build_planner_tools(working_state, writer))
    messages: list[Any] = [
        SystemMessage(content=PLANNER_PROMPT),
        HumanMessage(content=_planner_input(working_state, memory)),
    ]
    produced_messages: list[Any] = []

    writer(
        {
            "type": "plan_snapshot",
            "node": "planner",
            "plan_summary": working_state.get("plan_summary", ""),
            "todos": working_state.get("todos", []),
            "verification_commands": working_state.get("verification_commands", []),
            "attempts": working_state.get("attempts", 0),
        }
    )

    for _ in range(8):
        response = invoke_streaming(
            planner, messages, writer=writer, node="planner", state=working_state
        )
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_message = _execute_planner_tool(working_state, writer, call)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(
            AIMessage(
                content="planner stopped after the maximum supervisor tool loop count."
            )
        )

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = _last_ai_content(produced_messages)
    final_memory = build_layered_memory(working_state, node="planner")
    return {
        "plan_summary": working_state.get("plan_summary", ""),
        "todos": working_state.get("todos", []),
        "acceptance_criteria": working_state.get("acceptance_criteria", []),
        "verification_commands": working_state.get("verification_commands", []),
        "research_notes": working_state.get("research_notes", ""),
        "sources": working_state.get("sources", []),
        "agent_handoffs": working_state.get("agent_handoffs", []),
        "code_agent_summary": working_state.get("code_agent_summary", ""),
        "last_actor_summary": working_state.get("code_agent_summary", ""),
        "messages": produced_messages,
        "memory_snapshot": final_memory,
        "history_summary": final_memory.get("history_summary_store", {}).get(
            "history_summary", ""
        ),
        "metadata": metadata,
        "context_next_node": "verifier",
        **graph_context_updates(working_state, messages, "planner"),
    }


def verifier_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    memory = build_layered_memory(state, node="verifier")
    writer(memory_event(memory, node="verifier"))
    writer(
        {
            "type": "plan_snapshot",
            "node": "verifier",
            "plan_summary": state.get("plan_summary", ""),
            "todos": state.get("todos", []),
            "verification_commands": state.get("verification_commands", []),
        }
    )

    model = create_model()
    verifier = model.bind_tools(build_read_only_tools(state["runtime"]))
    from mokioclaw.tools.mcp_tools import emit_mcp_diagnostics

    emit_mcp_diagnostics(state["runtime"], writer)
    messages: list[Any] = [
        SystemMessage(content=VERIFIER_PROMPT),
        HumanMessage(content=_verifier_input(state, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    for _ in range(8):
        response = invoke_streaming(
            verifier, messages, writer=writer, node="verifier", state=state
        )
        produced_messages.append(response)
        messages.append(response)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            writer(
                {
                    "type": "tool_call",
                    "node": "verifier",
                    "name": call.get("name"),
                    "args": call.get("args", {}),
                }
            )
            tool_message = _execute_read_only_tool(state, call, on_retry=writer)
            event = _tool_result_event(tool_message, node="verifier")
            tool_events.append(event)
            writer(event)
            produced_messages.append(tool_message)
            messages.append(tool_message)
    else:
        produced_messages.append(
            AIMessage(
                content=json.dumps(
                    {
                        "passed": False,
                        "reason": "Verifier stopped after the maximum tool loop count.",
                        "checks": [],
                        "recommended_next_instruction": "Inspect the workspace and complete the unfinished task.",
                    },
                    ensure_ascii=False,
                )
            )
        )

    parsed = _extract_json(_last_ai_content(produced_messages)) or {
        "passed": False,
        "reason": "Verifier did not return valid JSON.",
        "checks": [
            {
                "name": "verifier_json",
                "passed": False,
                "detail": _last_ai_content(produced_messages)[:800],
            }
        ],
        "recommended_next_instruction": "Return valid verifier JSON after inspecting the result.",
    }
    checks = _normalize_checks(parsed.get("checks"))
    passed = bool(parsed.get("passed"))
    reason = str(parsed.get("reason") or "")
    recommended = str(parsed.get("recommended_next_instruction") or "")
    attempts = state.get("attempts", 0) + 1
    todos = [dict(todo) for todo in state.get("todos", [])]
    if passed:
        todos = [
            {
                **todo,
                "status": "completed"
                if todo.get("status") != "blocked"
                else todo.get("status", "blocked"),
                "note": todo.get("note") or "verified",
            }
            for todo in todos
        ]
        writer(
            {
                "type": "todo_update",
                "node": "verifier",
                "plan_summary": state.get("plan_summary", ""),
                "todos": todos,
                "verification_commands": state.get("verification_commands", []),
            }
        )
    last_error = (
        "" if passed else _format_verifier_error(reason, recommended, tool_events)
    )

    return {
        "messages": produced_messages,
        "verification_results": _tool_events_to_verification_results(tool_events),
        "verification_checks": checks,
        "verifier_summary": reason,
        "passed": passed,
        "attempts": attempts,
        "last_error": last_error,
        "todos": todos,
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get(
            "history_summary", ""
        ),
        "context_next_node": verifier_route(
            {**state, "passed": passed, "attempts": attempts}
        ),
        **graph_context_updates(state, messages, "verifier"),
    }


def context_monitor_node(state: MokioGraphState) -> dict[str, Any]:
    writer = _get_writer()
    budget = ContextBudget.from_runtime(state.get("runtime"))
    configured_limit = budget.window
    reserve = budget.output
    token_limit = budget.high
    token_count = estimate_context_tokens(state)
    should_compress = token_count >= token_limit
    next_node = state.get("context_next_node") or "verifier"
    event = {
        "type": "context_monitor",
        "scope": "graph",
        "estimation_method": "graph_state_estimate",
        "token_count": token_count,
        "token_limit": token_limit,
        "configured_token_limit": configured_limit,
        "reserved_tokens": reserve,
        "safety_tokens": budget.safety,
        "estimated": True,
        "should_compress": should_compress,
        "next_node": next_node,
        "message_count": len(state.get("messages", [])),
    }
    writer(event)
    return {
        "context_token_count": token_count,
        "context_token_limit": token_limit,
        "context_should_compress": should_compress,
        "context_next_node": next_node,
    }


def context_monitor_route(state: MokioGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: MokioGraphState) -> dict[str, Any]:
    from mokioclaw.core.context_compaction import compact_messages

    writer = _get_writer()
    before_tokens = state.get("context_token_count") or estimate_context_tokens(state)
    historical = [
        AIMessage(content=_message_text(m)) if isinstance(m, HumanMessage) else m
        for m in state.get("messages", [])
    ]
    try:
        model = create_model()
    except RunInterrupted:
        raise
    except Exception:
        model = None
    replacement, event = compact_messages(
        model,
        historical,
        state,
        budget=ContextBudget.from_runtime(state["runtime"]),
        node="graph",
        reason="graph_watermark",
        summarizer=lambda *_args: _compress_context_with_model(state, writer=writer),
    )
    summary = state.get("context_summary", "")
    after_tokens = estimate_context_tokens({**state, "messages": replacement})
    event.update(
        scope="graph",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        removed_messages=len(historical),
        next_node=state.get("context_next_node", "verifier"),
    )
    state["compression_events"] = (
        list(state.get("compression_events", []))[:-1] + [event]
    )[-20:]
    writer({**event, "type": "context_compression"})
    return {
        "messages": [RemoveMessage(id=REMOVE_ALL_MESSAGES), *replacement],
        "context_summary": summary,
        "history_summary": state.get("history_summary", summary),
        "context_version": state["runtime"].context_version,
        "context_token_count": after_tokens,
        "context_should_compress": False,
        "compression_events": state["compression_events"],
    }


def context_compressor_route(state: MokioGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def verifier_route(state: MokioGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def final_node(state: MokioGraphState) -> dict[str, Any]:
    status = "PASSED" if state.get("passed") else "FAILED"
    checks = "\n".join(
        f"- {check.get('name', 'check')}: {'PASS' if check.get('passed') else 'FAIL'} - {check.get('detail', '')}"
        for check in state.get("verification_checks", [])
    )
    todos = "\n".join(
        f"- [{todo.get('status', '')}] {todo.get('content', '')}"
        for todo in state.get("todos", [])
    )
    sources = "\n".join(
        f"- {source.get('title', '')}: {source.get('url', '')}"
        for source in state.get("sources", [])
    )
    compression_events = state.get("compression_events", [])
    compression_text = "(none)"
    if compression_events:
        latest = compression_events[-1]
        compression_text = (
            f"{len(compression_events)} compression(s); "
            f"latest {latest.get('before_tokens')} -> {latest.get('after_tokens')} tokens; "
            f"removed {latest.get('removed_messages')} message(s)"
        )
    final_answer = (
        f"LangGraph MultiAgent workflow finished: {status}\n\n"
        f"Plan: {state.get('plan_summary', '')}\n\n"
        f"Todos:\n{todos}\n\n"
        f"Research sources:\n{sources or '(none)'}\n\n"
        f"Verifier:\n{state.get('verifier_summary', '')}\n\n"
        f"Checks:\n{checks or '(none)'}\n\n"
        f"Context compression:\n{compression_text}\n\n"
        f"CodeAgent summary:\n{state.get('code_agent_summary') or state.get('last_actor_summary', '')}"
    )
    return {"final_answer": final_answer}


def get_context_token_limit() -> int:
    load_dotenv()
    raw = os.getenv("MOKIO_CONTEXT_TOKEN_LIMIT", str(DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: MokioGraphState) -> int:
    messages = list(state.get("messages", []))
    payload = build_layered_memory(state, node="context_monitor")
    payload_message = HumanMessage(
        content=json.dumps(payload, ensure_ascii=False, default=str)
    )
    from mokioclaw.core.context_budget import estimate_messages

    try:
        model = create_model()
    except RunInterrupted:
        raise
    except Exception:  # noqa: BLE001 - conservative estimate also works offline
        model = None
    return estimate_messages(model, messages + [payload_message], [])[0]


def _build_planner_tools(state: MokioGraphState, writer) -> list[StructuredTool]:
    registry = state["runtime"].subagent_registry or registered_subagents()
    return [
        StructuredTool.from_function(
            name="TodoWriteTool",
            func=lambda todos, acceptance_criteria, verification_commands, plan_summary="": (
                _todo_write_tool(
                    state,
                    writer,
                    todos,
                    acceptance_criteria,
                    verification_commands,
                    plan_summary,
                )
            ),
            description=(
                "Publish or revise plan state. Args: todos, acceptance_criteria, "
                "verification_commands, optional plan_summary."
            ),
        ),
        StructuredTool.from_function(
            name="CallSubagentTool",
            func=lambda subagent_name, instruction, run_in_background=False: (
                _call_registered_subagent_tool(
                    state,
                    writer,
                    subagent_name,
                    instruction,
                    run_in_background=run_in_background,
                )
            ),
            description=(
                "Delegate a bounded task to a registered serial Subagent. Registered Subagents: "
                + ", ".join(registry.names())
                + ". Args: subagent_name, instruction, optional run_in_background."
            ),
        ),
        StructuredTool.from_function(
            name="CallSearchAgentTool",
            func=lambda instruction, run_in_background=False: _call_search_agent_tool(
                state, writer, instruction, run_in_background=run_in_background
            ),
            description="Delegate research work to searchAgent. Args: instruction, optional run_in_background.",
        ),
        StructuredTool.from_function(
            name="CallCodeAgentTool",
            func=lambda instruction, run_in_background=False: _call_code_agent_tool(
                state, writer, instruction, run_in_background=run_in_background
            ),
            description="Delegate implementation work to codeAgent. Args: instruction, optional run_in_background.",
        ),
        StructuredTool.from_function(
            name="AgentResultTool",
            func=lambda run_id, wait_seconds=0: _agent_result_tool(
                state, writer, run_id, wait_seconds
            ),
            description="Inspect a background Subagent run and optionally wait up to 30 seconds. Args: run_id, optional wait_seconds.",
        ),
        StructuredTool.from_function(
            name="AgentCancelTool",
            func=lambda run_id: _agent_cancel_tool(state, run_id),
            description="Request cancellation of a background Subagent run. Args: run_id.",
        ),
    ]


def _call_registered_subagent_tool(
    state: MokioGraphState,
    writer,
    subagent_name: str,
    instruction: str,
    *,
    run_in_background: bool = False,
) -> dict[str, Any]:
    check_run(state["runtime"])
    name = str(subagent_name)
    handoff_id = str(uuid4())
    runtime = state["runtime"]
    if run_in_background:
        manager = runtime.subagent_task_manager
        if manager is None or not runtime.parent_run_id:
            return {
                "ok": False,
                "error_type": "daemon_required",
                "message": "run_in_background requires a daemon-managed Agent run",
            }
        source_registry = runtime.subagent_registry or registered_subagents()
        definition = source_registry.get(name)
        if definition is None:
            return {
                "ok": False,
                "error_type": "unknown_subagent",
                "message": f"unknown Subagent: {name}",
            }
        if not definition.background_safe:
            return {
                "ok": False,
                "error_type": "background_not_allowed",
                "message": f"Subagent {name} is not declared safe for background execution",
            }
        unsafe_tools = [
            tool_name
            for tool_name in definition.allowed_tools
            if (spec := tool_spec_for(tool_name)) is None or not spec.read_only
        ]
        if unsafe_tools:
            return {
                "ok": False,
                "error_type": "background_requires_read_only_tools",
                "message": (
                    f"Subagent {name} declares tools that are not registered as read-only: "
                    + ", ".join(sorted(unsafe_tools))
                ),
            }
        compatibility_runner = {
            "searchAgent": (run_search_agent, _DEFAULT_SEARCH_AGENT),
            "codeAgent": (run_code_agent, _DEFAULT_CODE_AGENT),
        }.get(name)
        if (
            compatibility_runner
            and compatibility_runner[0] is not compatibility_runner[1]
        ):
            source_registry = source_registry.copy()
            definition = replace(
                definition, runner=compatibility_runner[0], legacy_runner=True
            )
            source_registry.register(definition, replace=True)
        # Freeze only the fields this Subagent is permitted to read, then give it
        # a private RuntimeState with its own mutable caches and scope.
        context = {
            key: copy.deepcopy(state[key])
            for key in definition.context_fields
            if key in state
        }
        child_state = {"task": str(state.get("task", "")), **context}
        child_runtime = copy.copy(runtime)
        for attr in (
            "read_files",
            "tool_policies",
            "bash_deny_patterns",
            "bash_allow_patterns",
            "subagent_configs",
            "tool_cache",
            "mcp_servers",
            "mcp_discovery_errors",
        ):
            value = getattr(runtime, attr)
            setattr(
                child_runtime,
                attr,
                {} if attr in {"read_files", "tool_cache"} else copy.deepcopy(value),
            )
        child_runtime.active_agent_tools = None
        child_runtime.active_skill_tools = None
        child_runtime.active_skill_permissions = None
        child_runtime.active_skill_name = None
        child_runtime.active_skill_source = None
        child_runtime.active_subagent_budget = None
        from mokioclaw.core.run_control import RunControl

        child_runtime.run_control = RunControl()
        child_runtime.mcp_diagnostics = []
        child_runtime.subagent_applied_results = set()
        from mokioclaw.core.context_control import ContextControl

        child_runtime.context_control = ContextControl()
        child_runtime.context_replacements = {}
        child_runtime.context_checkpoint = {}
        child_runtime.context_version = 0
        child_runtime.context_request_sequence = 0
        child_runtime.subagent_registry = source_registry.copy()
        child_state["runtime"] = child_runtime

        def runner(child_writer, cancel_event):
            child_runtime.subagent_cancel_event = cancel_event
            from mokioclaw.core.run_control import CURRENT_RUN

            token = CURRENT_RUN.set(child_runtime.run_control)
            try:
                return run_subagent(
                    child_state,
                    name,
                    instruction,
                    writer=child_writer,
                    handoff_id=handoff_id,
                )
            finally:
                CURRENT_RUN.reset(token)
                child_runtime.run_control.close()

        writer(
            {
                "type": "handoff",
                "handoff_id": handoff_id,
                "from": "planner",
                "to": name,
                "instruction": instruction,
                "run_in_background": True,
            }
        )
        accepted = manager.submit(
            parent_run_id=runtime.parent_run_id,
            handoff_id=handoff_id,
            agent=name,
            instruction=instruction,
            runner=runner,
            parent_writer=runtime.subagent_parent_writer,
        )
        if not accepted.get("ok"):
            writer(
                {
                    "type": "handoff_result",
                    "handoff_id": handoff_id,
                    "from": name,
                    "to": "planner",
                    "result": accepted.get("message", "Background task was rejected"),
                    "ok": False,
                    "stop_reason": accepted.get("error_type", "submission_rejected"),
                }
            )
            return accepted
        writer(
            {
                "type": "subagent_task_queued",
                **{
                    key: accepted[key]
                    for key in ("subagent_run_id", "parent_run_id", "handoff_id")
                },
                "agent": name,
            }
        )
        handoff = {
            "handoff_id": handoff_id,
            "to_agent": name,
            "instruction": instruction,
            "subagent_run_id": accepted["subagent_run_id"],
            "status": "queued",
            "run_in_background": True,
        }
        state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
        return accepted
    writer(
        {
            "type": "handoff",
            "handoff_id": handoff_id,
            "from": "planner",
            "to": name,
            "instruction": instruction,
            "run_in_background": False,
        }
    )
    previous_registry = runtime.subagent_registry
    registry_override = None
    # Preserve the old monkeypatch hook while keeping the call inside the same
    # registry, budget, permission, and result-normalization path.
    compatibility_runner = {
        "searchAgent": (run_search_agent, _DEFAULT_SEARCH_AGENT),
        "codeAgent": (run_code_agent, _DEFAULT_CODE_AGENT),
    }.get(name)
    if compatibility_runner and compatibility_runner[0] is not compatibility_runner[1]:
        registry_override = (previous_registry or registered_subagents()).copy()
        definition = registry_override.get(name)
        if definition is not None:
            registry_override.register(
                replace(definition, runner=compatibility_runner[0], legacy_runner=True),
                replace=True,
            )
            runtime.subagent_registry = registry_override
    try:
        result = run_subagent(
            state, name, instruction, writer=writer, handoff_id=handoff_id
        )
    finally:
        runtime.subagent_registry = previous_registry
    summary = str(result.get("summary", ""))
    state_updates = result.get("state_updates", {})
    if isinstance(state_updates, dict):
        state.update(state_updates)
    handoff = {
        "handoff_id": handoff_id,
        "from_agent": "planner",
        "to_agent": name,
        "instruction": instruction,
        "result": summary,
        "ok": result.get("ok", False),
        "evidence": result.get("evidence", []),
        "unresolved": result.get("unresolved", []),
        "stop_reason": result.get("stop_reason"),
    }
    state["agent_handoffs"] = list(state.get("agent_handoffs", [])) + [handoff]
    writer(
        {
            "type": "handoff_result",
            "handoff_id": handoff_id,
            "from": name,
            "to": "planner",
            "result": summary,
            "evidence": result.get("evidence", []),
            "unresolved": result.get("unresolved", []),
            "ok": result.get("ok", False),
            "stop_reason": result.get("stop_reason"),
        }
    )
    response = {
        key: value
        for key, value in result.items()
        if key not in {"messages", "tool_events"}
    }
    if name == "searchAgent":
        response["sources"] = state.get("sources", [])
        response["queries"] = result.get("metadata", {}).get("queries", [])
    elif name == "codeAgent":
        response["todos"] = state.get("todos", [])
    return response


def _todo_write_tool(
    state: MokioGraphState,
    writer,
    todos: Any,
    acceptance_criteria: Any,
    verification_commands: Any,
    plan_summary: str = "",
) -> dict[str, Any]:
    result = write_todos(todos, acceptance_criteria, verification_commands)
    if result.get("ok"):
        state["plan_summary"] = (
            plan_summary or state.get("plan_summary") or "MultiAgent plan"
        )
        state["todos"] = _todo_items(result["todos"], existing=state.get("todos", []))
        state["acceptance_criteria"] = result["acceptance_criteria"]
        state["verification_commands"] = result["verification_commands"]
        persist_todos(
            state["runtime"],
            state["todos"],
            state["acceptance_criteria"],
            state["verification_commands"],
            state.get("plan_summary", ""),
        )
        writer(
            {
                "type": "plan_snapshot",
                "node": "planner",
                "plan_summary": state.get("plan_summary", ""),
                "todos": state.get("todos", []),
                "verification_commands": state.get("verification_commands", []),
                "acceptance_criteria": state.get("acceptance_criteria", []),
            }
        )
    return {
        **result,
        "plan_summary": state.get("plan_summary", ""),
        "todo_items": state.get("todos", []),
    }


def _call_search_agent_tool(
    state: MokioGraphState, writer, instruction: str, *, run_in_background: bool = False
) -> dict[str, Any]:
    return _call_registered_subagent_tool(
        state, writer, "searchAgent", instruction, run_in_background=run_in_background
    )


def _call_code_agent_tool(
    state: MokioGraphState, writer, instruction: str, *, run_in_background: bool = False
) -> dict[str, Any]:
    return _call_registered_subagent_tool(
        state, writer, "codeAgent", instruction, run_in_background=run_in_background
    )


def _agent_result_tool(
    state: MokioGraphState, writer, run_id: str, wait_seconds: float = 0
) -> dict[str, Any]:
    manager = state["runtime"].subagent_task_manager
    if manager is None:
        return {
            "ok": False,
            "error_type": "daemon_required",
            "message": "AgentResultTool requires a daemon-managed Agent run",
        }
    try:
        wait_seconds = min(30.0, max(0.0, float(wait_seconds)))
    except (TypeError, ValueError):
        wait_seconds = 0.0
    task = manager.get(str(run_id), wait_seconds=wait_seconds)
    if task is None:
        return {
            "ok": False,
            "error_type": "unknown_subagent_run",
            "run_id": str(run_id),
        }
    result = task.get("result")
    if task.get("status") in {
        "completed",
        "failed",
        "cancelled",
        "interrupted",
    } and isinstance(result, dict):
        applied = state["runtime"].subagent_applied_results
        if run_id not in applied:
            definition = (
                state["runtime"].subagent_registry or registered_subagents()
            ).get(str(task.get("agent", "")))
            updates = result.get("state_updates", {})
            if definition and isinstance(updates, dict):
                for key, value in updates.items():
                    if key in definition.state_update_fields:
                        if key == "sources" and isinstance(value, list):
                            merged = {
                                source.get("url", str(source)): source
                                for source in state.get("sources", [])
                            }
                            merged.update(
                                {
                                    source.get("url", str(source)): copy.deepcopy(
                                        source
                                    )
                                    for source in value
                                }
                            )
                            state[key] = list(merged.values())
                        elif key == "research_notes" and state.get(key) and value:
                            state[key] = str(value) + "\n\n" + str(state[key])
                        else:
                            state[key] = copy.deepcopy(value)
            applied.add(str(run_id))
            writer(
                {
                    "type": "handoff_result",
                    "handoff_id": task.get("handoff_id"),
                    "subagent_run_id": run_id,
                    "from": task.get("agent"),
                    "to": "planner",
                    "result": result.get("summary", ""),
                    "evidence": result.get("evidence", []),
                    "unresolved": result.get("unresolved", []),
                    "ok": bool(result.get("ok")),
                    "stop_reason": result.get("stop_reason"),
                    "state_updates_applied": True,
                }
            )
    writer(
        {
            "type": "subagent_task_status",
            "subagent_run_id": run_id,
            "parent_run_id": task.get("parent_run_id"),
            "handoff_id": task.get("handoff_id"),
            "agent": task.get("agent"),
            "status": task.get("status"),
        }
    )
    return {"ok": task.get("status") == "completed", **task}


def _agent_cancel_tool(state: MokioGraphState, run_id: str) -> dict[str, Any]:
    manager = state["runtime"].subagent_task_manager
    if manager is None:
        return {
            "ok": False,
            "error_type": "daemon_required",
            "message": "AgentCancelTool requires a daemon-managed Agent run",
        }
    return manager.cancel(str(run_id))


def _execute_planner_tool(
    state: MokioGraphState, writer, call: dict[str, Any]
) -> ToolMessage:
    check_run(state.get("runtime") if state else None)
    name = call.get("name", "")
    args = call.get("args") or {}
    writer({"type": "tool_call", "node": "planner", "name": name, "args": args})
    tools = {tool.name: tool for tool in _build_planner_tools(state, writer)}
    tool = tools.get(name)
    if tool is None:
        result = tool_failure_result(f"unknown tool: {name}", tool_name=name)
    else:
        try:
            result = execute_tool(
                state["runtime"],
                tool,
                args,
                on_retry=writer,
                on_event=writer,
                tool_call_id=call.get("id"),
            )
        except RunInterrupted:
            raise
        except Exception as exc:
            result = tool_failure_result(exc, tool_name=name)
    result = bound_tool_result(result, state["runtime"], tool_name=name, writer=writer)
    tool_message = ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )
    writer(_tool_result_event(tool_message, node="planner"))
    return tool_message


def _execute_read_only_tool(
    state: MokioGraphState, call: dict[str, Any], *, on_retry=None
) -> ToolMessage:
    check_run(state.get("runtime") if state else None)
    name = call.get("name", "")
    args = call.get("args") or {}
    tools = {tool.name: tool for tool in build_read_only_tools(state["runtime"])}
    tool = tools.get(name)
    if tool is None:
        result = tool_failure_result(f"unknown tool: {name}", tool_name=name)
    else:
        try:
            result = execute_tool(
                state["runtime"],
                tool,
                args,
                on_retry=on_retry,
                on_event=on_retry,
                tool_call_id=call.get("id"),
            )
        except RunInterrupted:
            raise
        except Exception as exc:
            result = tool_failure_result(exc, tool_name=name)
    result = bound_tool_result(
        result, state["runtime"], tool_name=name, writer=on_retry
    )
    return ToolMessage(
        content=json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )


def _compress_context_with_model(
    state: MokioGraphState, *, writer=None
) -> dict[str, Any]:
    from mokioclaw.core.context_compaction import _model_observations, _observations

    extract = _observations([[message] for message in state.get("messages", [])])
    try:
        return _model_observations(
            state,
            extract,
            writer,
            ContextBudget.from_runtime(state["runtime"]),
            model_factory=create_model,
        )
    except RunInterrupted:
        raise
    except Exception as exc:
        return _fallback_compression(state, error=f"{type(exc).__name__}: {exc}")


def _fallback_compression(state: MokioGraphState, *, error: str = "") -> dict[str, Any]:
    return {
        "summary": _short_text(
            "\n\n".join(
                [
                    state.get("context_summary", ""),
                    state.get("research_notes", ""),
                    state.get("code_agent_summary", ""),
                    state.get("verifier_summary", ""),
                    state.get("last_error", ""),
                ]
            ),
            2400,
        ),
        "active_goal": state.get("task", ""),
        "completed_work": state.get("code_agent_summary", ""),
        "open_todos": [
            todo.get("content", "")
            for todo in state.get("todos", [])
            if todo.get("status") != "completed"
        ],
        "important_files": _important_files_from_state(state),
        "tool_findings": _short_text(state.get("last_error", ""), 1200),
        "sources": [
            {"title": source.get("title", ""), "url": source.get("url", "")}
            for source in state.get("sources", [])
        ],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


def _important_files_from_state(state: MokioGraphState) -> list[str]:
    files: list[str] = []
    for command in state.get("verification_commands", []):
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", command))
    for text in [state.get("code_agent_summary", ""), state.get("last_error", "")]:
        files.extend(re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", text))
    seen: set[str] = set()
    deduped = []
    for item in files:
        normalized = item.strip("\"'")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped


def _planner_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append(
            "Session context for this multi-turn coding session:\n"
            + str(state.get("session_context", ""))
        )
    parts.append(
        "Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory)
    )
    return "\n\n".join(parts)


def _verifier_input(state: MokioGraphState, memory: dict[str, Any]) -> str:
    parts = [f"Task: {state['task']}"]
    if state.get("session_context"):
        parts.append(
            "Session context for this multi-turn coding session:\n"
            + str(state.get("session_context", ""))
        )
    parts.append(
        "Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory)
    )
    parts.append("Inspect the workspace with tools and return only verifier JSON.")
    return "\n\n".join(parts)


def _router_input(state: MokioGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _chat_input(state: MokioGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _default_plan(task: str) -> dict[str, Any]:
    if _is_amiya_task(task):
        return {
            "plan_summary": "Research Amiya from Arknights and build a sourced HTML character profile.",
            "todos": AMIYA_TODOS,
            "acceptance_criteria": AMIYA_CRITERIA,
            "verification_commands": AMIYA_COMMANDS,
        }
    return {
        "plan_summary": "Coordinate specialist agents to complete and verify the requested deliverable.",
        "todos": DEFAULT_TODOS,
        "acceptance_criteria": [
            "The requested deliverable exists.",
            "The verifier model confirms completion.",
        ],
        "verification_commands": [],
    }


def _apply_plan(state: MokioGraphState, plan: dict[str, Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = _todo_items(
        [str(item) for item in plan.get("todos", [])], existing=state.get("todos", [])
    )
    state["acceptance_criteria"] = [
        str(item) for item in plan.get("acceptance_criteria", [])
    ]
    state["verification_commands"] = _verification_commands_for_task(
        state["task"], plan
    )


def _verification_commands_for_task(task: str, parsed: dict[str, Any]) -> list[str]:
    if _is_amiya_task(task):
        return AMIYA_COMMANDS
    return [str(item) for item in parsed.get("verification_commands") or []]


def _todo_items(
    todos: list[str], *, existing: list[dict[str, Any]] | None = None
) -> list[TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[TodoItem] = []
    for idx, todo in enumerate(todos, start=1):
        previous = existing_by_content.get(todo, {})
        items.append(
            {
                "id": str(previous.get("id") or f"todo-{idx}"),
                "content": todo,
                "status": str(previous.get("status") or "pending"),
                "note": str(previous.get("note") or ""),
            }
        )
    return items


def _extract_json(text: str) -> dict[str, Any] | None:
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    raw = fenced.group(1) if fenced else text
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        parsed = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _coerce_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))


def _tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(tool_message.content))
    except json.JSONDecodeError:
        parsed = tool_message.content
    return {
        "type": "tool_result",
        "node": node,
        "name": tool_message.name,
        "tool_call_id": tool_message.tool_call_id,
        "result": parsed,
    }


def _tool_events_to_verification_results(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results = []
    for event in events:
        result = event.get("result", {})
        if not isinstance(result, dict):
            continue
        results.append(
            {
                "command": result.get("command") or event.get("name", ""),
                "ok": bool(result.get("ok")),
                "exit_code": result.get("exit_code"),
                "stdout": str(result.get("stdout", "")),
                "stderr": str(result.get("stderr") or result.get("error", "")),
            }
        )
    return results


def _normalize_checks(raw: Any) -> list[VerificationCheck]:
    if not isinstance(raw, list):
        return []
    checks: list[VerificationCheck] = []
    for item in raw:
        if isinstance(item, dict):
            checks.append(
                {
                    "name": str(item.get("name") or "check"),
                    "passed": bool(item.get("passed")),
                    "detail": str(item.get("detail") or ""),
                }
            )
    return checks


def _format_verifier_error(
    reason: str, recommended: str, tool_events: list[dict[str, Any]]
) -> str:
    event_text = json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )


def _join_notes(existing: str, new: str) -> str:
    if not existing:
        return new
    if not new:
        return existing
    return existing + "\n\n" + new


def _dedupe_sources(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped = []
    for source in sources:
        url = str(source.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _trim_handoffs(handoffs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for handoff in handoffs[-6:]:
        trimmed.append(
            {
                "from_agent": handoff.get("from_agent", ""),
                "to_agent": handoff.get("to_agent", ""),
                "handoff_id": handoff.get("handoff_id", ""),
                "instruction": _short_text(str(handoff.get("instruction", "")), 500),
                "result": _short_text(str(handoff.get("result", "")), 700),
                "ok": handoff.get("ok"),
                "evidence": handoff.get("evidence", []),
                "unresolved": handoff.get("unresolved", []),
                "stop_reason": handoff.get("stop_reason"),
            }
        )
    return trimmed


def _short_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""


def _todos_text(todos: list[dict[str, Any]]) -> str:
    return "\n".join(
        f"- {todo.get('id', '')} [{todo.get('status', '')}] {todo.get('content', '')} {todo.get('note', '')}".strip()
        for todo in todos
    )


def _list_text(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items)


def _is_amiya_task(task: str) -> bool:
    lowered = task.lower()
    return (
        "阿米娅" in task
        or "amiya" in lowered
        or "arknights" in lowered
        or "明日方舟" in task
    )


def _get_writer():
    try:
        raw_writer = get_stream_writer()
    except RuntimeError:
        raw_writer = lambda _: None
    from mokioclaw.core.run_control import CURRENT_RUN

    control = CURRENT_RUN.get()

    def write(event):
        if control is not None and control.event_sink is not None:
            control.event_sink(event)
        raw_writer(event)

    return write
