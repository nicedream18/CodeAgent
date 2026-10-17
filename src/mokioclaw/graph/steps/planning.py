from __future__ import annotations

from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.graph import nodes as api


def planner_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    writer = api._get_writer()
    working_state: api.MokioGraphState = {**state}
    state["runtime"].foreground_state = working_state
    if not working_state.get("repo_explored"):
        from mokioclaw.tools.grep_tool import _iter_files

        source_files = (
            p
            for p in _iter_files(
                state["runtime"].workspace,
                workspace=state["runtime"].workspace.resolve(),
            )
            if p.name not in {"TODO.md", "NOTEPAD.md", "HISTORY_SUMMARY.md", "AGENTS.md"}
        )
        if next(source_files, None) is not None:
            result = api.run_subagent(
                working_state,
                "repoAgent",
                "Identify the existing implementation, conventions, and tests needed for this task before planning.",
                writer=writer,
            )
            working_state.update(result.get("state_updates", {}))
    if not working_state.get("todos"):
        api._apply_plan(working_state, api._default_plan(working_state["task"]))
        api.persist_todos(
            working_state["runtime"],
            working_state.get("todos", []),
            working_state.get("acceptance_criteria", []),
            working_state.get("verification_commands", []),
            working_state.get("plan_summary", ""),
        )

    memory = api.build_layered_memory(working_state, node="planner")
    writer(api.memory_event(memory, node="planner"))
    model = api.create_role_model(api.create_model, state["runtime"], "planner")
    planner = model.bind_tools(api._build_planner_tools(working_state, writer))
    messages: list[api.Any] = [
        api.SystemMessage(content=api.PLANNER_PROMPT),
        api.HumanMessage(content=api._planner_input(working_state, memory)),
    ]
    produced_messages: list[api.Any] = []

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

    conversation = ToolConversation(
        messages,
        state=working_state,
        writer=writer,
        node="planner",
        invoke=api.invoke_streaming,
        produced=produced_messages,
    )
    for response, tool_calls in conversation.rounds(8, lambda: planner):
        if not getattr(response, "tool_calls", None):
            break
        with conversation.batch(tool_calls) as pending_calls:
            for call in pending_calls:
                tool_message = api._execute_planner_tool(working_state, writer, call)
                conversation.append(tool_message)
    else:
        conversation.finish(
            model,
            "The delegation phase has ended. Return a concise factual supervisor summary "
            "of completed work, observed checks and remaining work. Do not request more tools.",
        )

    metadata = dict(working_state.get("metadata", {}))
    metadata["planner_raw"] = api._last_ai_content(produced_messages)
    final_memory = api.build_layered_memory(working_state, node="planner")
    return {
        "repo_context": working_state.get("repo_context", ""),
        "repo_explored": working_state.get("repo_explored", False),
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
        "history_summary": final_memory.get("history_summary_store", {}).get("history_summary", ""),
        "metadata": metadata,
        "context_next_node": "verifier",
        **api.graph_context_updates(working_state, messages, "planner"),
    }


def _build_planner_tools(state: api.MokioGraphState, writer) -> list[api.StructuredTool]:
    registry = state["runtime"].subagent_registry or api.registered_subagents()
    code_definition = registry.get("codeAgent")
    code_budget = (
        state["runtime"]
        .subagent_configs.get("codeAgent", {})
        .get("max_tool_calls", code_definition.max_tool_calls if code_definition else 10)
    )
    tools = [
        api.StructuredTool.from_function(
            name="CallRepoAgentTool",
            func=lambda instruction: api._call_registered_subagent_tool(
                state, writer, "repoAgent", instruction
            ),
            description="Explore existing local code, entry points and tests read-only before implementation.",
        ),
        api.StructuredTool.from_function(
            name="TodoWriteTool",
            func=lambda todos, acceptance_criteria, verification_commands, plan_summary="": (
                api._todo_write_tool(
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
        api.StructuredTool.from_function(
            name="CallSubagentTool",
            func=lambda subagent_name, instruction, run_in_background=False: (
                api._call_registered_subagent_tool(
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
        api.StructuredTool.from_function(
            name="CallSearchAgentTool",
            func=lambda instruction, run_in_background=False: api._call_search_agent_tool(
                state, writer, instruction, run_in_background=run_in_background
            ),
            description="Delegate research work to searchAgent. Args: instruction, optional run_in_background.",
        ),
        api.StructuredTool.from_function(
            name="CallCodeAgentTool",
            func=lambda instruction, run_in_background=False: api._call_code_agent_tool(
                state, writer, instruction, run_in_background=run_in_background
            ),
            description=f"Delegate implementation and targeted verification to codeAgent; its handoff budget is {code_budget} tool calls, including TODO updates. Reuse the repository map and keep small tasks to one implementation-and-check TODO. Args: instruction, optional run_in_background.",
        ),
        api.StructuredTool.from_function(
            name="AgentResultTool",
            func=lambda run_id, wait_seconds=0: api._agent_result_tool(
                state, writer, run_id, wait_seconds
            ),
            description="Inspect a background Subagent run and optionally wait up to 30 seconds. Args: run_id, optional wait_seconds.",
        ),
        api.StructuredTool.from_function(
            name="AgentCancelTool",
            func=lambda run_id: api._agent_cancel_tool(state, run_id),
            description="Request cancellation of a background Subagent run. Args: run_id.",
        ),
    ]

    if state["runtime"].evaluation_mode:
        tools = [
            t
            for t in tools
            if t.name not in {"CallSearchAgentTool", "AgentResultTool", "AgentCancelTool"}
        ]
    return tools


def _todo_write_tool(
    state: api.MokioGraphState,
    writer,
    todos: api.Any,
    acceptance_criteria: api.Any,
    verification_commands: api.Any,
    plan_summary: str = "",
) -> dict[str, api.Any]:
    result = api.write_todos(todos, acceptance_criteria, verification_commands)
    if result.get("ok"):
        state["plan_summary"] = plan_summary or state.get("plan_summary") or "MultiAgent plan"
        state["todos"] = api._todo_items(result["todos"], existing=state.get("todos", []))
        state["acceptance_criteria"] = result["acceptance_criteria"]
        state["verification_commands"] = result["verification_commands"]
        api.persist_todos(
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


def _execute_planner_tool(
    state: api.MokioGraphState, writer, call: dict[str, api.Any]
) -> api.ToolMessage:
    api.check_run(state.get("runtime") if state else None)
    name = call.get("name", "")
    args = call.get("args") or {}
    writer({"type": "tool_call", "node": "planner", "name": name, "args": args})
    tools = {tool.name: tool for tool in api._build_planner_tools(state, writer)}
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
                on_retry=writer,
                on_event=writer,
                tool_call_id=call.get("id"),
            )
        except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
            raise
        except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            result = api.tool_failure_result(exc, tool_name=name)
    result = api.bound_tool_result(result, state["runtime"], tool_name=name, writer=writer)
    tool_message = api.ToolMessage(
        content=api.json.dumps(result, ensure_ascii=False),
        name=name,
        tool_call_id=call.get("id") or f"{name}-call",
    )
    writer(api._tool_result_event(tool_message, node="planner"))
    return tool_message


def _planner_input(state: api.MokioGraphState, memory: dict[str, api.Any]) -> str:
    parts = [
        f"Task: {state['task']}",
        f"Required Skill: {state['runtime'].explicit_skill or 'none'}",
        f"Attempt: {state.get('attempts', 0) + 1}",
    ]
    if state.get("session_context"):
        parts.append(
            "Session context for this multi-turn coding session:\n"
            + str(state.get("session_context", ""))
        )
    parts.append("Layered memory snapshot:\n" + api.format_layered_memory_for_prompt(memory))
    if state.get("repo_explored"):
        parts.append(
            "Repository exploration already completed. Use repo_context to plan and delegate implementation. "
            "The code agent must read exact source before editing; do not ask the repo agent to copy the same "
            "method again merely to prepare that handoff. Request new exploration only for a specific missing dependency."
        )
    return "\n\n".join(parts)


def _default_plan(task: str) -> dict[str, api.Any]:
    return {
        "plan_summary": "Coordinate specialist agents to complete and verify the requested deliverable.",
        "todos": api.DEFAULT_TODOS,
        "acceptance_criteria": [
            "The requested deliverable exists.",
            "The verifier model confirms completion.",
        ],
        "verification_commands": [],
    }


def _apply_plan(state: api.MokioGraphState, plan: dict[str, api.Any]) -> None:
    state["plan_summary"] = str(plan.get("plan_summary", ""))
    state["todos"] = api._todo_items(
        [str(item) for item in plan.get("todos", [])], existing=state.get("todos", [])
    )
    state["acceptance_criteria"] = [str(item) for item in plan.get("acceptance_criteria", [])]
    state["verification_commands"] = api._verification_commands_for_task(state["task"], plan)


def _todo_items(
    todos: list[str], *, existing: list[dict[str, api.Any]] | None = None
) -> list[api.TodoItem]:
    existing_by_content = {todo.get("content", ""): todo for todo in existing or []}
    items: list[api.TodoItem] = []
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
