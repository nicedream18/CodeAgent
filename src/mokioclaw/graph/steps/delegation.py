from __future__ import annotations

from mokioclaw.graph import nodes as api


def _call_registered_subagent_tool(
    state: api.MokioGraphState,
    writer,
    subagent_name: str,
    instruction: str,
    *,
    run_in_background: bool = False,
) -> dict[str, api.Any]:
    api.check_run(state["runtime"])
    name = str(subagent_name)
    handoff_id = str(api.uuid4())
    runtime = state["runtime"]
    if run_in_background:
        manager = runtime.subagent_task_manager
        if manager is None or not runtime.parent_run_id:
            return {
                "ok": False,
                "error_type": "daemon_required",
                "message": "run_in_background requires a daemon-managed Agent run",
            }
        source_registry = runtime.subagent_registry or api.registered_subagents()
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
            if (spec := api.tool_spec_for(tool_name)) is None or not spec.read_only
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
            "searchAgent": (api.run_search_agent, api._DEFAULT_SEARCH_AGENT),
            "codeAgent": (api.run_code_agent, api._DEFAULT_CODE_AGENT),
        }.get(name)
        if compatibility_runner and compatibility_runner[0] is not compatibility_runner[1]:
            source_registry = source_registry.copy()
            definition = api.replace(definition, runner=compatibility_runner[0], legacy_runner=True)
            source_registry.register(definition, replace=True)
        # Freeze only the fields this Subagent is permitted to read, then give it
        # a private RuntimeState with its own mutable caches and scope.
        context = {
            key: api.copy.deepcopy(state[key]) for key in definition.context_fields if key in state
        }
        child_state = {"task": str(state.get("task", "")), **context}
        child_runtime = api.copy.copy(runtime)
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
                {} if attr in {"read_files", "tool_cache"} else api.copy.deepcopy(value),
            )
        child_runtime.active_agent_tools = None
        child_runtime.active_skill_tools = None
        child_runtime.active_skill_permissions = None
        child_runtime.active_skill_name = None
        child_runtime.active_skill_source = None
        child_runtime.active_subagent_budget = None
        from mokioclaw.core.run_control import RunControl

        child_runtime.run_control = RunControl()
        child_runtime.loop_guard = None
        child_runtime.loop_stream_id = ""
        child_runtime.loop_tool_call_id = ""
        child_runtime.instructions = None
        child_runtime.model_metrics = {}
        child_runtime.mcp_loaded_tools = {}
        child_runtime.hook_context = []
        child_runtime.foreground_state = None
        child_runtime.completion_budget = None
        child_runtime.run_id = f"background-{api.uuid4().hex}"
        child_runtime.run_control.run_id = child_runtime.run_id
        child_runtime.run_control.runtime = child_runtime
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
                from mokioclaw.core.loop_guard import LoopRecovery, guard_for
                from mokioclaw.core.loop_guard import event as loop_event

                while True:
                    try:
                        return api.run_subagent(
                            child_state, name, instruction, writer=child_writer, handoff_id=handoff_id,
                        )
                    except LoopRecovery as signal:
                        child_state.update(signal.state)
                        if guard_for(child_runtime).recover():
                            child_writer(loop_event(child_runtime, "replan", signal.decision.reason))
                            child_state["last_error"] = "Change strategy after loop protection: " + signal.decision.reason
                            continue
                        child_writer(loop_event(child_runtime, "stop", signal.decision.reason))
                        return {"ok": False, "stop_reason": signal.reason,
                                "summary": signal.decision.reason,
                                "loop_guard": guard_for(child_runtime).snapshot()}

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
                    key: accepted[key] for key in ("subagent_run_id", "parent_run_id", "handoff_id")
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
    definition = (runtime.subagent_registry or api.registered_subagents()).get(name)
    base = runtime.agent_profiles.get(name, {}).get("base", name)
    if definition is not None and base == "codeAgent":
        # An implementation attempt invalidates previous testing, even if it later
        # returns partial work or a correction/cancellation interrupts the handoff.
        state["testing_required"] = True
        state["test_report"] = {}
    previous_registry = runtime.subagent_registry
    registry_override = None
    # Preserve the old monkeypatch hook while keeping the call inside the same
    # registry, budget, permission, and result-normalization path.
    compatibility_runner = {
        "searchAgent": (api.run_search_agent, api._DEFAULT_SEARCH_AGENT),
        "codeAgent": (api.run_code_agent, api._DEFAULT_CODE_AGENT),
    }.get(name)
    if compatibility_runner and compatibility_runner[0] is not compatibility_runner[1]:
        registry_override = (previous_registry or api.registered_subagents()).copy()
        definition = registry_override.get(name)
        if definition is not None:
            registry_override.register(
                api.replace(definition, runner=compatibility_runner[0], legacy_runner=True),
                replace=True,
            )
            runtime.subagent_registry = registry_override
    try:
        result = api.run_subagent(state, name, instruction, writer=writer, handoff_id=handoff_id)
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
        key: value for key, value in result.items() if key not in {"messages", "tool_events"}
    }
    if name == "searchAgent":
        response["sources"] = state.get("sources", [])
        response["queries"] = result.get("metadata", {}).get("queries", [])
    elif name == "codeAgent":
        response["todos"] = state.get("todos", [])
    return response


def _call_search_agent_tool(
    state: api.MokioGraphState, writer, instruction: str, *, run_in_background: bool = False
) -> dict[str, api.Any]:
    return api._call_registered_subagent_tool(
        state, writer, "searchAgent", instruction, run_in_background=run_in_background
    )


def _call_code_agent_tool(
    state: api.MokioGraphState, writer, instruction: str, *, run_in_background: bool = False
) -> dict[str, api.Any]:
    return api._call_registered_subagent_tool(
        state, writer, "codeAgent", instruction, run_in_background=run_in_background
    )


def _agent_result_tool(
    state: api.MokioGraphState, writer, run_id: str, wait_seconds: float = 0
) -> dict[str, api.Any]:
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
            definition = (state["runtime"].subagent_registry or api.registered_subagents()).get(
                str(task.get("agent", ""))
            )
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
                                    source.get("url", str(source)): api.copy.deepcopy(source)
                                    for source in value
                                }
                            )
                            state[key] = list(merged.values())
                        elif key == "research_notes" and state.get(key) and value:
                            state[key] = str(value) + "\n\n" + str(state[key])
                        else:
                            state[key] = api.copy.deepcopy(value)
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


def _agent_cancel_tool(state: api.MokioGraphState, run_id: str) -> dict[str, api.Any]:
    manager = state["runtime"].subagent_task_manager
    if manager is None:
        return {
            "ok": False,
            "error_type": "daemon_required",
            "message": "AgentCancelTool requires a daemon-managed Agent run",
        }
    return manager.cancel(str(run_id))


def _dedupe_sources(sources: list[dict[str, api.Any]]) -> list[dict[str, api.Any]]:
    seen: set[str] = set()
    deduped = []
    for source in sources:
        url = str(source.get("url", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(source)
    return deduped


def _trim_handoffs(handoffs: list[dict[str, api.Any]]) -> list[dict[str, api.Any]]:
    trimmed = []
    for handoff in handoffs[-6:]:
        trimmed.append(
            {
                "from_agent": handoff.get("from_agent", ""),
                "to_agent": handoff.get("to_agent", ""),
                "handoff_id": handoff.get("handoff_id", ""),
                "instruction": api._short_text(str(handoff.get("instruction", "")), 500),
                "result": api._short_text(str(handoff.get("result", "")), 700),
                "ok": handoff.get("ok"),
                "evidence": handoff.get("evidence", []),
                "unresolved": handoff.get("unresolved", []),
                "stop_reason": handoff.get("stop_reason"),
            }
        )
    return trimmed
