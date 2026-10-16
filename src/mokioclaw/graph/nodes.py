# ruff: noqa: E402, F401
# Compatibility facade imports implementations after defining shared dependencies.
from __future__ import annotations

import copy  # noqa: F401
import json  # noqa: F401
import os  # noqa: F401
import re  # noqa: F401
from dataclasses import replace  # noqa: F401
from typing import Any  # noqa: F401
from uuid import uuid4  # noqa: F401

from dotenv import load_dotenv  # noqa: F401
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)  # noqa: F401
from langchain_core.tools import StructuredTool  # noqa: F401
from langgraph.config import get_stream_writer  # noqa: F401
from langgraph.graph.message import REMOVE_ALL_MESSAGES  # noqa: F401

from mokioclaw.agents.code_agent import run_code_agent as _DEFAULT_CODE_AGENT  # noqa: F401
from mokioclaw.agents.registry import registered_subagents, run_subagent  # noqa: F401
from mokioclaw.agents.search_agent import run_search_agent as _DEFAULT_SEARCH_AGENT  # noqa: F401
from mokioclaw.core.run_control import RunInterrupted, check_run  # noqa: F401
from mokioclaw.core.run_inbox import tool_call_batch  # noqa: F401
from mokioclaw.providers.generation import ModelGenerationStopped  # noqa: F401

# Kept as patchable compatibility hooks for callers/tests that replace the
# original specialist runners.
run_code_agent = _DEFAULT_CODE_AGENT
run_search_agent = _DEFAULT_SEARCH_AGENT
from mokioclaw.core.context_budget import ContextBudget, ContextBudgetError  # noqa: F401
from mokioclaw.core.context_compaction import graph_context_updates  # noqa: F401
from mokioclaw.core.tool_output import bound_tool_result  # noqa: F401
from mokioclaw.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
)
from mokioclaw.graph.state import MokioGraphState, TodoItem, VerificationCheck  # noqa: F401
from mokioclaw.prompts.stage3 import PLANNER_PROMPT, VERIFIER_PROMPT  # noqa: F401
from mokioclaw.providers.model_policy import create_role_model  # noqa: F401
from mokioclaw.providers.openai_provider import create_model, invoke_streaming  # noqa: F401
from mokioclaw.tools import build_read_only_tools  # noqa: F401
from mokioclaw.tools.execution import execute_tool, tool_failure_result  # noqa: F401
from mokioclaw.tools.registry import tool_spec_for  # noqa: F401
from mokioclaw.tools.todo_tool import persist_todos, write_todos  # noqa: F401

DEFAULT_CONTEXT_TOKEN_LIMIT = 400000

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
For workflow requests also return complexity (simple or complex), target_files
(exact workspace paths), and cross_module (boolean). Only label simple when a
single file and its desired change are explicit, with no external tools or dependencies.
"""

CHAT_RESPONDER_PROMPT = """You are MokioClaw's lightweight chat node.

Answer the user directly and concisely. Do not claim that you read files,
searched the web, ran commands, edited files, or inspected the workspace.
If the user asks for work requiring tools or project context, say that it
should be handled by the workflow route.

If session context is provided, you may use the recent conversation summary to
answer conversational follow-ups, but do not invent workspace facts.
"""


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False, default=str)


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


def _join_notes(existing: str, new: str) -> str:
    if not existing:
        return new
    if not new:
        return existing
    return existing + "\n\n" + new


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


def _get_writer():
    try:
        raw_writer = get_stream_writer()
    except RuntimeError:

        def raw_writer(_):
            return None

    from mokioclaw.core.run_control import CURRENT_RUN

    control = CURRENT_RUN.get()

    def write(event):
        event.setdefault("event_id", uuid4().hex)
        event.setdefault("run_id", getattr(control, "run_id", ""))
        event.setdefault(
            "context_version",
            getattr(getattr(control, "runtime", None), "context_version", 0),
        )
        if control is not None and control.event_sink is not None:
            control.event_sink(event)
        raw_writer(event)

    return write


# Public compatibility facade; implementation dependencies remain patchable here.
from mokioclaw.graph.steps.compression import (
    _compress_context_with_model as _compress_context_with_model,
)  # noqa: F401
from mokioclaw.graph.steps.compression import _fallback_compression as _fallback_compression  # noqa: F401
from mokioclaw.graph.steps.compression import (
    _important_files_from_state as _important_files_from_state,
)  # noqa: F401
from mokioclaw.graph.steps.compression import context_compressor_node as context_compressor_node  # noqa: F401
from mokioclaw.graph.steps.compression import context_compressor_route as context_compressor_route  # noqa: F401
from mokioclaw.graph.steps.compression import context_monitor_node as context_monitor_node  # noqa: F401
from mokioclaw.graph.steps.compression import context_monitor_route as context_monitor_route  # noqa: F401
from mokioclaw.graph.steps.compression import estimate_context_tokens as estimate_context_tokens  # noqa: F401
from mokioclaw.graph.steps.compression import get_context_token_limit as get_context_token_limit  # noqa: F401
from mokioclaw.graph.steps.delegation import _agent_cancel_tool as _agent_cancel_tool  # noqa: F401
from mokioclaw.graph.steps.delegation import _agent_result_tool as _agent_result_tool  # noqa: F401
from mokioclaw.graph.steps.delegation import _call_code_agent_tool as _call_code_agent_tool  # noqa: F401
from mokioclaw.graph.steps.delegation import (
    _call_registered_subagent_tool as _call_registered_subagent_tool,
)  # noqa: F401
from mokioclaw.graph.steps.delegation import _call_search_agent_tool as _call_search_agent_tool  # noqa: F401
from mokioclaw.graph.steps.delegation import _dedupe_sources as _dedupe_sources  # noqa: F401
from mokioclaw.graph.steps.delegation import _trim_handoffs as _trim_handoffs  # noqa: F401
from mokioclaw.graph.steps.final import _list_text as _list_text  # noqa: F401
from mokioclaw.graph.steps.final import _todos_text as _todos_text  # noqa: F401
from mokioclaw.graph.steps.final import final_node as final_node  # noqa: F401
from mokioclaw.graph.steps.planning import _apply_plan as _apply_plan  # noqa: F401
from mokioclaw.graph.steps.planning import _build_planner_tools as _build_planner_tools  # noqa: F401
from mokioclaw.graph.steps.planning import _default_plan as _default_plan  # noqa: F401
from mokioclaw.graph.steps.planning import _execute_planner_tool as _execute_planner_tool  # noqa: F401
from mokioclaw.graph.steps.planning import _planner_input as _planner_input  # noqa: F401
from mokioclaw.graph.steps.planning import _todo_items as _todo_items  # noqa: F401
from mokioclaw.graph.steps.planning import _todo_write_tool as _todo_write_tool  # noqa: F401
from mokioclaw.graph.steps.planning import planner_node as planner_node  # noqa: F401
from mokioclaw.graph.steps.review import _execute_read_only_tool as _execute_read_only_tool  # noqa: F401
from mokioclaw.graph.steps.review import _format_verifier_error as _format_verifier_error  # noqa: F401
from mokioclaw.graph.steps.review import _normalize_checks as _normalize_checks  # noqa: F401
from mokioclaw.graph.steps.review import (
    _tool_events_to_verification_results as _tool_events_to_verification_results,
)  # noqa: F401
from mokioclaw.graph.steps.review import (
    _verification_commands_for_task as _verification_commands_for_task,
)  # noqa: F401
from mokioclaw.graph.steps.review import _verifier_input as _verifier_input  # noqa: F401
from mokioclaw.graph.steps.review import verifier_node as verifier_node  # noqa: F401
from mokioclaw.graph.steps.review import verifier_route as verifier_route  # noqa: F401
from mokioclaw.graph.steps.routing import _chat_input as _chat_input  # noqa: F401
from mokioclaw.graph.steps.routing import _coerce_confidence as _coerce_confidence  # noqa: F401
from mokioclaw.graph.steps.routing import _router_input as _router_input  # noqa: F401
from mokioclaw.graph.steps.routing import chat_responder_node as chat_responder_node  # noqa: F401
from mokioclaw.graph.steps.routing import intent_route_fn as intent_route_fn  # noqa: F401
from mokioclaw.graph.steps.routing import intent_router_node as intent_router_node  # noqa: F401
