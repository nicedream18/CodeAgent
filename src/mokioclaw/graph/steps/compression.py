from __future__ import annotations

from mokioclaw.graph import nodes as api


def context_monitor_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    writer = api._get_writer()
    budget = api.ContextBudget.from_runtime(state.get("runtime"))
    configured_limit = budget.window
    reserve = budget.output
    token_limit = budget.high
    token_count = api.estimate_context_tokens(state)
    enabled = getattr(state.get("runtime"), "context_compression_enabled", True)
    should_compress = bool(enabled and token_count >= token_limit)
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
        "compression_enabled": enabled,
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


def context_monitor_route(state: api.MokioGraphState) -> str:
    if state.get("context_should_compress"):
        return "context_compressor"
    return state.get("context_next_node") or "verifier"


def context_compressor_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    from mokioclaw.core.context_compaction import compact_messages

    writer = api._get_writer()
    before_tokens = state.get("context_token_count") or api.estimate_context_tokens(state)
    historical = [
        api.AIMessage(content=api._message_text(m)) if isinstance(m, api.HumanMessage) else m
        for m in state.get("messages", [])
    ]
    try:
        model = api.create_role_model(api.create_model, state["runtime"], "context_compressor")
    except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
        raise
    except Exception:  # noqa: BLE001 — fault boundary; control signals are handled separately
        model = None
    replacement, event = compact_messages(
        model,
        historical,
        state,
        budget=api.ContextBudget.from_runtime(state["runtime"]),
        node="graph",
        reason="graph_watermark",
        summarizer=lambda *_args: api._compress_context_with_model(state, writer=writer),
    )
    summary = state.get("context_summary", "")
    after_tokens = api.estimate_context_tokens({**state, "messages": replacement})
    event.update(
        scope="graph",
        before_tokens=before_tokens,
        after_tokens=after_tokens,
        removed_messages=len(historical),
        next_node=state.get("context_next_node", "verifier"),
    )
    state["compression_events"] = (list(state.get("compression_events", []))[:-1] + [event])[-20:]
    writer({**event, "type": "context_compression"})
    return {
        "messages": [api.RemoveMessage(id=api.REMOVE_ALL_MESSAGES), *replacement],
        "context_summary": summary,
        "history_summary": state.get("history_summary", summary),
        "context_version": state["runtime"].context_version,
        "context_token_count": after_tokens,
        "context_should_compress": False,
        "compression_events": state["compression_events"],
    }


def context_compressor_route(state: api.MokioGraphState) -> str:
    return state.get("context_next_node") or "verifier"


def get_context_token_limit() -> int:
    api.load_dotenv()
    raw = api.os.getenv("MOKIO_CONTEXT_TOKEN_LIMIT", str(api.DEFAULT_CONTEXT_TOKEN_LIMIT))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return api.DEFAULT_CONTEXT_TOKEN_LIMIT
    return value if value > 0 else api.DEFAULT_CONTEXT_TOKEN_LIMIT


def estimate_context_tokens(state: api.MokioGraphState) -> int:
    messages = list(state.get("messages", []))
    payload = api.build_layered_memory(state, node="context_monitor")
    payload_message = api.HumanMessage(
        content=api.json.dumps(payload, ensure_ascii=False, default=str)
    )
    from mokioclaw.core.context_budget import estimate_messages

    try:
        model = api.create_role_model(api.create_model, state["runtime"], "context_monitor")
    except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
        raise
    except Exception:  # noqa: BLE001 - conservative estimate also works offline
        model = None
    return estimate_messages(model, messages + [payload_message], [])[0]


def _compress_context_with_model(state: api.MokioGraphState, *, writer=None) -> dict[str, api.Any]:
    from mokioclaw.core.context_compaction import _model_observations, _observations

    extract = _observations([[message] for message in state.get("messages", [])])
    try:
        return _model_observations(
            state,
            extract,
            writer,
            api.ContextBudget.from_runtime(state["runtime"]),
            model_factory=lambda: api.create_role_model(
                api.create_model, state["runtime"], "context_compressor"
            ),
        )
    except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
        raise
    except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
        return api._fallback_compression(state, error=f"{type(exc).__name__}: {exc}")


def _fallback_compression(state: api.MokioGraphState, *, error: str = "") -> dict[str, api.Any]:
    return {
        "summary": api._short_text(
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
        "important_files": api._important_files_from_state(state),
        "tool_findings": api._short_text(state.get("last_error", ""), 1200),
        "sources": [
            {"title": source.get("title", ""), "url": source.get("url", "")}
            for source in state.get("sources", [])
        ],
        "next_steps": state.get("context_next_node", ""),
        "risks": error,
    }


def _important_files_from_state(state: api.MokioGraphState) -> list[str]:
    files: list[str] = []
    for command in state.get("verification_commands", []):
        files.extend(api.re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", command))
    for text in [state.get("code_agent_summary", ""), state.get("last_error", "")]:
        files.extend(api.re.findall(r"[\w./\\-]+\.(?:py|html|css|js|json|md|txt)", text))
    seen: set[str] = set()
    deduped = []
    for item in files:
        normalized = item.strip("\"'")
        if normalized and normalized not in seen:
            seen.add(normalized)
            deduped.append(normalized)
    return deduped
