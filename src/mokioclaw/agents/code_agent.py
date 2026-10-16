from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from mokioclaw.core.run_control import RunInterrupted, check_run
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_output import bound_tool_result
from mokioclaw.graph.memory import (
    build_layered_memory,
    format_layered_memory_for_prompt,
    memory_event,
)
from mokioclaw.graph.state import MokioGraphState
from mokioclaw.prompts.stage3 import CODE_AGENT_PROMPT
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools import build_tools
from mokioclaw.tools.execution import (
    execute_tool,
    reserve_subagent_tool_call,
    tool_failure_result,
)
from mokioclaw.tools.skills import skill_summaries
from mokioclaw.tools.todo_tool import persist_todos, update_todo

Writer = Callable[[dict[str, Any]], None]


def run_code_agent(
    state: MokioGraphState,
    instruction: str,
    *,
    writer: Writer | None = None,
    max_loops: int = 10,
) -> dict[str, Any]:
    runtime = state["runtime"]
    check_run(runtime)
    todos = [dict(todo) for todo in state.get("todos", [])]
    writer = writer or (lambda _: None)
    memory = build_layered_memory({**state, "todos": todos}, node="codeAgent")
    writer(memory_event(memory, node="codeAgent"))
    model = create_model()
    registered_tools = build_tools(runtime)
    from mokioclaw.tools.mcp_tools import emit_mcp_diagnostics

    emit_mcp_diagnostics(runtime, writer)
    if runtime.active_agent_tools is not None:
        registered_tools = [
            tool
            for tool in registered_tools
            if tool.name in runtime.active_agent_tools
            or (tool.name.startswith("MCP_") and "MCP_*" in runtime.active_agent_tools)
        ]
    for error in runtime.mcp_discovery_errors:
        writer({"type": "mcp_discovery_error", **error})
    code_agent = model.bind_tools(registered_tools + [_build_todo_update_tool(todos)])

    writer(
        {
            "type": "plan_snapshot",
            "node": "codeAgent",
            "plan_summary": state.get("plan_summary", ""),
            "todos": todos,
            "verification_commands": state.get("verification_commands", []),
        }
    )

    messages = [
        SystemMessage(content=CODE_AGENT_PROMPT),
        HumanMessage(content=_code_agent_input(state, instruction, memory)),
    ]
    produced_messages: list[Any] = []
    tool_events: list[dict[str, Any]] = []

    budget_exhausted = False
    final_response_allowance = 1 if runtime.active_subagent_budget is not None else 0
    for _ in range(max_loops + final_response_allowance):
        response = invoke_streaming(
            code_agent, messages, writer=writer, node="codeAgent", state=state
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
                    "node": "codeAgent",
                    "name": call.get("name"),
                    "args": call.get("args", {}),
                }
            )
            tool_result, todos = execute_code_agent_tool(
                runtime, todos, call, on_retry=writer, on_event=writer
            )
            state["todos"] = todos
            event = tool_result_event(tool_result, node="codeAgent")
            tool_events.append(event)
            writer(event)
            if call.get("name") == "TodoUpdateTool":
                persist_todos(
                    runtime,
                    todos,
                    state.get("acceptance_criteria", []),
                    state.get("verification_commands", []),
                    state.get("plan_summary", ""),
                )
                writer(
                    {
                        "type": "todo_update",
                        "node": "codeAgent",
                        "plan_summary": state.get("plan_summary", ""),
                        "todos": todos,
                        "verification_commands": state.get("verification_commands", []),
                    }
                )
            produced_messages.append(tool_result)
            messages.append(tool_result)
            if (
                runtime.active_subagent_budget is not None
                and runtime.active_subagent_budget.exhausted
            ):
                budget_exhausted = True
                break
        if budget_exhausted:
            break
    else:
        message = (
            "codeAgent stopped after the final response allowance."
            if final_response_allowance
            else "codeAgent stopped after the maximum tool loop count; verifier will inspect current files."
        )
        produced_messages.append(AIMessage(content=message))

    summary = _last_ai_content(produced_messages)
    return {
        "ok": not budget_exhausted,
        "summary": summary,
        "todos": todos or state.get("todos", []),
        "messages": messages[2:],
        "tool_events": tool_events,
        "stop_reason": "tool_budget_exhausted" if budget_exhausted else None,
        "unresolved": (
            ["Subagent tool-call budget exhausted; implementation may be incomplete."]
            if budget_exhausted
            else []
        ),
    }


def execute_code_agent_tool(
    runtime: RuntimeState,
    todos: list[dict[str, str]],
    call: dict[str, Any],
    *,
    on_retry=None,
    on_event=None,
):
    check_run(runtime)
    name = call.get("name", "")
    args = call.get("args") or {}
    if name == "TodoUpdateTool":
        tool = _build_todo_update_tool(todos)
        result = execute_tool(
            runtime,
            tool,
            args,
            on_retry=on_retry,
            on_event=on_event,
            tool_call_id=call.get("id"),
        )
    else:
        tools = {tool.name: tool for tool in build_tools(runtime)}
        tool = tools.get(name)
        if tool is None:
            budget_failure = reserve_subagent_tool_call(
                runtime,
                name,
                str(call.get("id") or f"{name}-call"),
                on_event=on_event,
            )
            result = budget_failure or tool_failure_result(
                f"unknown tool: {name}", tool_name=name
            )
        else:
            try:
                result = execute_tool(
                    runtime,
                    tool,
                    args,
                    on_retry=on_retry,
                    on_event=on_event,
                    tool_call_id=call.get("id"),
                )
            except RunInterrupted:
                raise
            except Exception as exc:
                result = tool_failure_result(exc, tool_name=name)
    tool_call_id = call.get("id") or f"{name}-call"
    return ToolMessage(
        content=json.dumps(
            bound_tool_result(
                result, runtime, tool_name=name, writer=on_event or on_retry
            ),
            ensure_ascii=False,
        ),
        name=name,
        tool_call_id=tool_call_id,
    ), todos


def tool_result_event(tool_message: ToolMessage, *, node: str) -> dict[str, Any]:
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


def _build_todo_update_tool(todos: list[dict[str, str]]) -> StructuredTool:
    def apply(todo_id, status, note=""):
        result = update_todo(todos, todo_id, status, note)
        if result.get("ok"):
            todos[:] = result["todos"]
        return result

    return StructuredTool.from_function(
        name="TodoUpdateTool",
        func=apply,
        description="Update one existing todo status. Args: todo_id, status, optional note.",
    )


def _code_agent_input(
    state: MokioGraphState, instruction: str, memory: dict[str, Any]
) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Planner instruction:\n{instruction}",
    ]
    if state.get("session_context"):
        parts.append(
            "Session context for this multi-turn coding session:\n"
            + str(state.get("session_context", ""))
        )
    parts.append(
        "Discovered Skills (load a relevant one with LoadSkillTool):\n"
        + skill_summaries(state["runtime"])
    )
    parts.append(
        "Layered memory snapshot:\n" + format_layered_memory_for_prompt(memory)
    )
    return "\n\n".join(parts)


def _last_ai_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if isinstance(message, ToolMessage):
            continue
        content = getattr(message, "content", "")
        if content:
            return str(content)
    return ""
