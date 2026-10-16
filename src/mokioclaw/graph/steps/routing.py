from __future__ import annotations

from mokioclaw.graph import nodes as api


def intent_router_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    writer = api._get_writer()
    if state.get("runtime") and state["runtime"].explicit_skill:
        writer(
            {
                "type": "intent_decision",
                "route": "workflow",
                "confidence": 1.0,
                "reason": "Explicit Skill selected",
            }
        )
        return {
            "intent_route": "workflow",
            "intent_reason": "Explicit Skill selected",
            "intent_confidence": 1.0,
        }
    route = "workflow"
    reason = "router fallback: default to workflow"
    confidence = 0.0
    parsed = {}
    try:
        response = api.invoke_streaming(
            api.create_role_model(api.create_model, state.get("runtime"), "intent_router"),
            [
                api.SystemMessage(content=api.INTENT_ROUTER_PROMPT),
                api.HumanMessage(content=api._router_input(state)),
            ],
            writer=writer,
            node="intent_router",
            state=state,
        )
        parsed = api._extract_json(str(response.content)) or {}
        candidate = str(parsed.get("route", "")).strip().lower()
        parsed_confidence = api._coerce_confidence(parsed.get("confidence"))
        if candidate in {"chat", "workflow"} and parsed_confidence >= 0.55:
            route = candidate
            confidence = parsed_confidence
            reason = str(parsed.get("reason") or "")
        else:
            reason = str(parsed.get("reason") or "router returned low-confidence or invalid route")
            confidence = parsed_confidence
    except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
        raise
    except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
        reason = f"router error: {type(exc).__name__}: {exc}"

    event = {
        "type": "intent_decision",
        "route": route,
        "reason": reason,
        "confidence": confidence,
    }
    writer(event)
    return {
        "workflow_hint": {
            "complexity": parsed.get("complexity", "complex"),
            "target_files": parsed.get("target_files", []),
            "cross_module": parsed.get("cross_module", True),
            "confidence": confidence,
        },
        "intent_route": route,
        "intent_reason": reason,
        "intent_confidence": confidence,
    }


def intent_route_fn(state: api.MokioGraphState) -> str:
    return "chat_responder" if state.get("intent_route") == "chat" else "planner"


def chat_responder_node(state: api.MokioGraphState) -> dict[str, api.Any]:
    writer = api._get_writer()
    try:
        response = api.invoke_streaming(
            api.create_role_model(api.create_model, state.get("runtime"), "chat_responder"),
            [
                api.SystemMessage(content=api.CHAT_RESPONDER_PROMPT),
                api.HumanMessage(content=api._chat_input(state)),
            ],
            writer=writer,
            node="chat_responder",
            state=state,
        )
        text = str(getattr(response, "content", "") or "").strip()
    except (api.RunInterrupted, api.ContextBudgetError, api.ModelGenerationStopped):
        raise
    except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
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


def _router_input(state: api.MokioGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("latest_user_corrections"):
        parts.append(
            "Latest user corrections:\n"
            + api.json.dumps(state["latest_user_corrections"], ensure_ascii=False)
        )
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _chat_input(state: api.MokioGraphState) -> str:
    parts = [f"User input:\n{state.get('task', '')}"]
    if state.get("latest_user_corrections"):
        parts.append(
            "Latest user corrections:\n"
            + api.json.dumps(state["latest_user_corrections"], ensure_ascii=False)
        )
    if state.get("session_context"):
        parts.append("Session context:\n" + str(state.get("session_context", "")))
    return "\n\n".join(parts)


def _coerce_confidence(value: api.Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, confidence))
