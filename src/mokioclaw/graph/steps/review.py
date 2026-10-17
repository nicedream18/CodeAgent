from __future__ import annotations

from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.graph import nodes as api


def verifier_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    writer = api._get_writer()
    runtime = state["runtime"]
    runtime.foreground_state = state
    completion = getattr(runtime, "completion_budget", None)
    if completion:
        completion.phase = "verification"
    memory = api.build_layered_memory(state, node="verifier")
    writer(api.memory_event(memory, node="verifier"))
    writer(
        {
            "type": "plan_snapshot",
            "node": "verifier",
            "plan_summary": state.get("plan_summary", ""),
            "todos": state.get("todos", []),
            "verification_commands": state.get("verification_commands", []),
        }
    )

    model = api.create_role_model(api.create_model, state["runtime"], "verifier")
    verifier = model.bind_tools(api.build_read_only_tools(state["runtime"]))
    from mokioclaw.tools.mcp_tools import emit_mcp_diagnostics

    emit_mcp_diagnostics(state["runtime"], writer)
    messages: list[api.Any] = [
        api.SystemMessage(content=api.VERIFIER_PROMPT),
        api.HumanMessage(content=api._verifier_input(state, memory)),
    ]
    produced_messages: list[api.Any] = []
    tool_events: list[dict[str, api.Any]] = []

    conversation = ToolConversation(
        messages,
        state=state,
        writer=writer,
        node="verifier",
        invoke=api.invoke_streaming,
        produced=produced_messages,
    )
    for response, tool_calls in conversation.rounds(8, lambda: verifier):
        if not getattr(response, "tool_calls", None):
            break
        with conversation.batch(tool_calls) as pending_calls:
            for call in pending_calls:
                writer(
                    {
                        "type": "tool_call",
                        "node": "verifier",
                        "name": call.get("name"),
                        "args": call.get("args", {}),
                    }
                )
                tool_message = api._execute_read_only_tool(state, call, on_retry=writer)
                event = api._tool_result_event(tool_message, node="verifier")
                tool_events.append(event)
                writer(event)
                conversation.append(tool_message)
    else:
        report = conversation.finish(
            model,
            "The inspection phase has ended; no more tools are available. Return only verifier "
            "JSON with passed, reason, checks and recommended_next_instruction. Judge only observed "
            "workspace evidence and actual check results; report missing evidence as a failure.",
        )
        if report is None:
            produced_messages.append(
                api.AIMessage(
                    content=api.json.dumps(
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

    parsed = api._extract_json(api._last_ai_content(produced_messages)) or {
        "passed": False,
        "reason": "Verifier did not return valid JSON.",
        "checks": [
            {
                "name": "verifier_json",
                "passed": False,
                "detail": api._last_ai_content(produced_messages)[:800],
            }
        ],
        "recommended_next_instruction": "Return valid verifier JSON after inspecting the result.",
    }
    checks = api._normalize_checks(parsed.get("checks"))
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
    last_error = "" if passed else api._format_verifier_error(reason, recommended, tool_events)

    result = {
        "messages": produced_messages,
        "verification_results": api._tool_events_to_verification_results(tool_events),
        "verification_checks": checks,
        "verifier_summary": reason,
        "passed": passed,
        "attempts": attempts,
        "last_error": last_error,
        "todos": todos,
        "memory_snapshot": memory,
        "history_summary": memory.get("history_summary_store", {}).get("history_summary", ""),
        "context_next_node": api.verifier_route({**state, "passed": passed, "attempts": attempts}),
        **api.graph_context_updates(state, messages, "verifier"),
    }
    if completion and completion.draining and not passed:
        from mokioclaw.providers.model_policy import RunBudgetExceeded

        error = RunBudgetExceeded(
            "Reserved completion phase could not verify the task", "completion_verification_failed"
        )
        error.state = {key: value for key, value in state.items() if key not in {"runtime", "messages"}}
        error.state.update({key: value for key, value in result.items() if key != "messages"})
        raise error
    return result


def verifier_route(state: api.MokioGraphState) -> str:
    if state.get("passed"):
        return "final"
    if state.get("attempts", 0) >= state.get("max_attempts", 3):
        return "final"
    return "planner"


def _execute_read_only_tool(
    state: api.MokioGraphState, call: dict[str, api.Any], *, on_retry=None
) -> api.ToolMessage:
    api.check_run(state.get("runtime") if state else None)
    name = call.get("name", "")
    args = call.get("args") or {}
    tools = {tool.name: tool for tool in api.build_read_only_tools(state["runtime"])}
    tool = tools.get(name)
    if tool is None:
        result = api.tool_failure_result(f"unknown tool: {name}", tool_name=name)
        result["available_tools"] = sorted(tools)
    else:
        try:
            result = api.execute_tool(
                state["runtime"],
                tool,
                args,
                on_retry=on_retry,
                on_event=on_retry,
                tool_call_id=call.get("id"),
            )
        except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
            raise
        except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            result = api.tool_failure_result(exc, tool_name=name)
    result = api.bound_tool_result(result, state["runtime"], tool_name=name, writer=on_retry)
    return api.ToolMessage(
        content=api.json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )


def _verifier_input(state: api.MokioGraphState, memory: dict[str, api.Any]) -> str:
    parts = [f"Task: {state['task']}"]
    if state.get("session_context"):
        parts.append(
            "Session context for this multi-turn coding session:\n"
            + str(state.get("session_context", ""))
        )
    parts.append("Layered memory snapshot:\n" + api.format_layered_memory_for_prompt(memory))
    parts.append("Inspect the workspace with tools and return only verifier JSON.")
    return "\n\n".join(parts)


def _verification_commands_for_task(task: str, parsed: dict[str, api.Any]) -> list[str]:
    return [str(item) for item in parsed.get("verification_commands") or []]


def _tool_events_to_verification_results(
    events: list[dict[str, api.Any]],
) -> list[dict[str, api.Any]]:
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


def _normalize_checks(raw: api.Any) -> list[api.VerificationCheck]:
    if not isinstance(raw, list):
        return []
    checks: list[api.VerificationCheck] = []
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
    reason: str, recommended: str, tool_events: list[dict[str, api.Any]]
) -> str:
    event_text = api.json.dumps(tool_events[-3:], ensure_ascii=False, default=str)[:1600]
    return (
        f"Verifier failed: {reason}\n"
        f"Recommended next instruction: {recommended}\n"
        f"Recent verifier tool events:\n{event_text}"
    )
