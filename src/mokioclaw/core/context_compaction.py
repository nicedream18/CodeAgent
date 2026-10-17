"""Budgeted compaction at complete model/tool message boundaries.

The model summarizes observations only. Current requirements and task state are
copied by the runtime, never accepted from the summarizer as authority.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.completion_budget import CompletionYield
from mokioclaw.core.context_budget import ContextBudget, ContextBudgetError, snapshot
from mokioclaw.core.run_control import RunInterrupted
from mokioclaw.core.tool_output import encoded
from mokioclaw.providers.generation import ModelGenerationStopped

FACT_FIELDS = (
    "task",
    "explicit_skill",
    "latest_user_corrections",
    "plan_summary",
    "todos",
    "acceptance_criteria",
    "verification_commands",
    "last_error",
    "context_next_node",
    "attempts",
    "repo_explored",
    "repo_context",
    "code_agent_summary",
)


def critical_facts(state: dict[str, Any]) -> dict[str, Any]:
    facts = {key: copy.deepcopy(state[key]) for key in FACT_FIELDS if state.get(key) is not None}
    facts["sources"] = [
        {"title": source.get("title", ""), "url": source.get("url", "")}
        for source in state.get("sources", [])
    ]
    facts["background_tasks"] = [
        {
            key: item[key]
            for key in ("subagent_run_id", "instruction", "status", "handoff_id")
            if key in item
        }
        for item in state.get("agent_handoffs", [])
        if item.get("subagent_run_id")
    ]
    runtime = state.get("runtime")
    if runtime is not None:
        facts["applied_subagent_results"] = sorted(runtime.subagent_applied_results)
        from mokioclaw.core.instructions import instructions

        facts["project_instructions"] = instructions(runtime).manifest()
        if runtime.run_control and runtime.run_control.inbox.path:
            facts["inbox_reference"] = str(
                runtime.run_control.inbox.path.relative_to(runtime.workspace)
            )
    return facts


def message_groups(messages: list[Any]) -> tuple[list[Any], list[list[Any]]]:
    protected, groups = [], []
    pending: set[str] = set()
    for message in messages:
        if isinstance(message, (SystemMessage, HumanMessage)):
            if pending:
                raise ContextBudgetError("cannot compact an incomplete tool-call group")
            protected.append(message)
        elif isinstance(message, ToolMessage):
            if message.tool_call_id not in pending:
                raise ContextBudgetError("orphan tool result in model context")
            groups[-1].append(message)
            pending.remove(message.tool_call_id)
        else:
            if pending:
                raise ContextBudgetError("missing tool results before next model message")
            groups.append([message])
            pending = {str(call["id"]) for call in getattr(message, "tool_calls", [])}
    if pending:
        raise ContextBudgetError("cannot compact before all tool results are collected")
    return protected, groups


def _observations(groups: list[list[Any]]) -> dict[str, Any]:
    # A bounded extract is the input to the summarizer, not the full transcript.
    findings = []
    for group in groups[-12:]:
        for message in group[-4:]:
            text = str(message.content)
            findings.append(
                {
                    "type": message.type,
                    "name": getattr(message, "name", ""),
                    "content": text if len(text) <= 700 else text[:340] + "\n…\n" + text[-340:],
                }
            )
    return {"findings": findings}


def validated_observations(candidate: Any, fallback: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate, dict) or not isinstance(candidate.get("summary"), str):
        return {
            "summary": "Deterministic history extract",
            **fallback,
            "degraded": True,
        }
    # No task/todo/source/command fields supplied by the model are authoritative.
    return {"summary": candidate["summary"][:2400], **fallback, "degraded": False}


def _model_observations(state, extract, writer, budget, model_factory=None) -> dict[str, Any]:
    from mokioclaw.prompts.stage4 import CONTEXT_COMPRESSION_PROMPT
    from mokioclaw.providers.openai_provider import create_model, invoke_streaming

    messages = [
        SystemMessage(content=CONTEXT_COMPRESSION_PROMPT),
        HumanMessage(content=encoded(extract)),
    ]
    from mokioclaw.providers.model_policy import create_role_model

    model = create_role_model(model_factory or create_model, state["runtime"], "context_compressor")
    # Fit a bounded extract before invoking the same guarded transport. No
    # recursive compaction of the compressor is permitted.
    while snapshot(model, messages, budget, []).input_tokens > budget.target:
        findings = extract["findings"]
        if not findings:
            raise ContextBudgetError("summary prompt cannot fit the model input budget")
        extract = {"findings": findings[len(findings) // 2 + 1 :]}
        messages[1] = HumanMessage(content=encoded(extract))
    response = invoke_streaming(
        model,
        messages,
        state=state,
        writer=writer,
        node="context_compressor",
        allow_compaction=False,
    )
    content = str(response.content).strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0]
    return json.loads(content)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        temporary.write_text(encoded(value), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def compact_messages(
    model,
    messages: list[Any],
    state: dict[str, Any],
    *,
    budget: ContextBudget,
    writer=None,
    node="",
    reason="automatic",
    request_id=None,
    summarizer=None,
) -> tuple[list[Any], dict[str, Any]]:
    before = snapshot(model, messages, budget)
    protected, groups = message_groups(messages)
    if not groups:
        if before.input_tokens > budget.input_limit:
            raise ContextBudgetError(
                "required instructions exceed the input budget; enlarge the window or reduce the task"
            )
        return list(messages), {
            "status": "noop",
            "before_tokens": before.input_tokens,
            "after_tokens": before.input_tokens,
        }
    extract = _observations(groups)
    try:
        candidate = (summarizer or _model_observations)(state, extract, writer, budget)
        observations = validated_observations(candidate, extract)
    except (RunInterrupted, ModelGenerationStopped):
        raise
    except (Exception, CompletionYield) as exc:  # noqa: BLE001 - keep deterministic memory on summary failure/reservation
        observations = {
            "summary": "Deterministic history extract",
            **extract,
            "degraded": True,
            "degradation_reason": f"{type(exc).__name__}: {exc}"[:240],
        }
    facts = critical_facts(state)
    runtime = state.get("runtime")
    version = getattr(runtime, "context_version", 0) + 1
    # Facts remain exact in durable storage. Prompt projections may use a
    # bounded catalogue for large source lists, with a readable full reference.
    facts_path = None
    if runtime is not None:
        scope = re.sub(r"[^A-Za-z0-9_-]", "-", node or "model")[:48]
        active = runtime.active_subagent_budget
        if active is not None:
            scope += "-" + active.handoff_id[:12]
        facts_path = Path(".mokioclaw") / "context" / scope / f"facts-{uuid4().hex}.json"
        _atomic_json(runtime.workspace / facts_path, facts)
    prompt_facts = dict(facts)
    for key in ("repo_context", "code_agent_summary"):
        if len(str(prompt_facts.get(key, ""))) > 1600:
            prompt_facts[key] = {
                "summary": str(prompt_facts[key])[:1600],
                "full_facts_path": str(facts_path),
            }
    for key in ("sources", "background_tasks", "applied_subagent_results"):
        if len(encoded(prompt_facts.get(key, []))) > 4000:
            prompt_facts[key] = {
                "count": len(facts[key]),
                "full_facts_path": str(facts_path),
                "items": facts[key][-8:],
            }
            while len(encoded(prompt_facts[key])) > 4000 and prompt_facts[key]["items"]:
                prompt_facts[key]["items"].pop(0)
    payload = {
        "type": "mokio_context_summary",
        "context_version": version,
        "facts": prompt_facts,
        "observations": observations,
    }
    if facts_path:
        payload["full_facts_path"] = facts_path.as_posix()
    # Retain the most recent complete tool group when it fits the target.
    recent = groups[-1] if any(isinstance(m, ToolMessage) for m in groups[-1]) else []
    if recent:
        last_user = max(
            (index for index, item in enumerate(messages) if isinstance(item, HumanMessage)),
            default=-1,
        )
        recent_position = next(index for index, item in enumerate(messages) if item is recent[0])
        if recent_position < last_user:
            recent = []
    replacement = protected + [AIMessage(content=encoded(payload))] + recent
    if snapshot(model, replacement, budget).input_tokens > budget.target:
        payload["observations"] = {
            "summary": observations["summary"][:900],
            "degraded": observations.get("degraded", False),
        }
        # Prefer the last complete observation over reaching a soft target. A
        # head/tail history summary can omit exactly the code just requested.
        replacement = protected + [AIMessage(content=encoded(payload))] + recent
        if snapshot(model, replacement, budget).input_tokens > budget.input_limit:
            replacement = protected + [AIMessage(content=encoded(payload))]
    after = snapshot(model, replacement, budget)
    if after.input_tokens > budget.input_limit:
        raise ContextBudgetError(
            "required task, acceptance criteria and schemas cannot fit after compaction"
        )
    if after.input_tokens >= before.input_tokens and reason == "automatic":
        if before.input_tokens > budget.input_limit:
            raise ContextBudgetError("compaction did not reduce an oversized context")
        return list(messages), {
            "status": "noop",
            "before_tokens": before.input_tokens,
            "after_tokens": before.input_tokens,
        }
    event = {
        "type": "context_compression",
        "event_id": uuid4().hex,
        "scope": "run",
        "agent": node,
        "reason": reason,
        "request_id": request_id,
        "status": "applied",
        "context_version": version,
        "before_tokens": before.input_tokens,
        "after_tokens": after.input_tokens,
        "removed_messages": sum(len(group) for group in groups)
        - (len(recent) if len(replacement) > len(protected) + 1 else 0),
        "target_tokens": budget.target,
        "target_reached": after.input_tokens <= budget.target,
        "estimation_method": after.estimation_method,
        "estimated": True,
        "degraded": observations.get("degraded", False),
        "summary": observations["summary"][:900],
    }
    if runtime is not None:
        root = runtime.workspace / facts_path.parent
        _atomic_json(root / f"version-{version}.json", payload)
        _atomic_json(
            root / "current.json",
            {"context_version": version, "file": f"version-{version}.json"},
        )
        # Publish a complete version before updating in-memory active state.
        runtime.context_version = version
        if runtime.context_control is not None:
            with runtime.context_control.lock:
                runtime.context_control.version = version
        runtime.context_replacements[node] = replacement[len(protected) :]
        state["context_summary"] = encoded(payload)
        state["history_summary"] = encoded(payload)
        state["context_version"] = version
        state["compression_events"] = (list(state.get("compression_events", [])) + [event])[-20:]
        runtime.context_checkpoint = {
            key: copy.deepcopy(state[key])
            for key in (
                "context_summary",
                "history_summary",
                "context_version",
                "compression_events",
            )
        }
        runtime.context_checkpoint["messages"] = replacement[len(protected) :]
        # A background search must not overwrite the parent's durable summary.
        if node not in {"searchAgent", "context_compressor"}:
            from mokioclaw.graph.memory import persist_history_summary

            persist_history_summary(runtime, encoded(payload))
    if writer:
        writer(event)
    return replacement, event


def prepare_model_context(
    model,
    messages: list[Any],
    state=None,
    *,
    writer=None,
    node="",
    allow_compaction=True,
):
    state = state if state is not None else {}
    runtime = state.get("runtime")
    budget = ContextBudget.from_runtime(runtime)
    before = snapshot(model, messages, budget)
    control = getattr(runtime, "context_control", None)
    request = (
        control.take()
        if (control and allow_compaction and not getattr(runtime, "active_subagent_budget", None))
        else None
    )
    try:
        if allow_compaction and (request or before.input_tokens >= budget.high):
            replacement, result = compact_messages(
                model,
                messages,
                state,
                budget=budget,
                writer=writer,
                node=node,
                reason="manual" if request else "automatic",
                request_id=request["request_id"] if request else None,
            )
            messages[:] = replacement
            if request:
                completed = control.finish(
                    request["request_id"],
                    result["status"],
                    **{k: v for k, v in result.items() if k not in {"status", "request_id"}},
                )
                if writer:
                    writer({**completed, "type": "context_compact_request"})
        current = snapshot(model, messages, budget)
        if current.input_tokens > budget.input_limit:
            raise ContextBudgetError("request exceeds context budget before model invocation")
    except (RunInterrupted, ModelGenerationStopped):
        raise
    except Exception as exc:
        event = {
            "type": "context_compression_failed",
            "event_id": uuid4().hex,
            "scope": "run",
            "agent": node,
            "reason": "manual" if request else "automatic",
            "error": str(exc)[:500],
            "status": "failed",
        }
        if request:
            event.update(
                control.finish(request["request_id"], "failed", ok=False, error=event["error"])
            )
        if writer:
            writer(event)
        raise
    if runtime is not None:
        runtime.context_request_sequence += 1
    event = {
        "type": "context_budget",
        "event_id": uuid4().hex,
        **current.as_dict(),
        "scope": "run",
        "agent": node,
        "request_sequence": getattr(runtime, "context_request_sequence", 0),
        "context_version": getattr(runtime, "context_version", 0),
        "run_id": getattr(runtime, "parent_run_id", None),
    }
    if writer:
        writer(event)
    return budget, current


def graph_context_updates(state: dict[str, Any], messages: list[Any], node: str) -> dict[str, Any]:
    from langchain_core.messages import RemoveMessage
    from langgraph.graph.message import REMOVE_ALL_MESSAGES

    runtime = state["runtime"]
    if node not in runtime.context_replacements:
        return {}
    runtime.context_replacements.pop(node)
    return {
        "messages": [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            *[m for m in messages if not isinstance(m, (SystemMessage, HumanMessage))],
        ],
        **{
            key: state[key]
            for key in (
                "context_summary",
                "history_summary",
                "context_version",
                "compression_events",
            )
            if key in state
        },
    }
