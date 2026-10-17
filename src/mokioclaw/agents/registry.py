from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from mokioclaw.agents.code_agent import run_code_agent
from mokioclaw.agents.repo_agent import run_repo_agent
from mokioclaw.agents.search_agent import run_search_agent
from mokioclaw.core.context_budget import ContextBudgetError
from mokioclaw.core.run_control import RunInterrupted, check_run
from mokioclaw.core.run_inbox import RunRedirected
from mokioclaw.core.state import RuntimeState, SubagentToolBudget
from mokioclaw.graph.state import MokioGraphState
from mokioclaw.providers.generation import ModelGenerationStopped
from mokioclaw.providers.model_policy import RunBudgetExceeded


@dataclass(frozen=True)
class EvidenceItem:
    kind: str
    label: str
    reference: str = ""
    details: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "label": self.label,
            "reference": self.reference,
            "details": self.details,
        }


@dataclass(frozen=True)
class SubagentInput:
    task: str
    instruction: str
    runtime: RuntimeState
    context: Mapping[str, Any]

    def legacy_state(self) -> dict[str, Any]:
        """Build an isolated state projection for legacy Python runners."""
        return {
            "task": self.task,
            **copy.deepcopy(dict(self.context)),
            "runtime": self.runtime,
        }


@dataclass
class SubagentResult:
    ok: bool
    summary: str = ""
    evidence: list[EvidenceItem] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    state_updates: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        result = {
            "ok": self.ok,
            "summary": self.summary,
            "evidence": [item.as_dict() for item in self.evidence],
            "unresolved": list(self.unresolved),
            "state_updates": copy.deepcopy(self.state_updates),
            "stop_reason": self.stop_reason,
        }
        if self.metadata:
            result["metadata"] = copy.deepcopy(self.metadata)
        return result


LegacyRunner = Callable[..., dict[str, Any]]
ContractRunner = Callable[..., SubagentResult | dict[str, Any]]
Runner = LegacyRunner | ContractRunner


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    runner: Runner
    allowed_tools: frozenset[str]
    max_tool_calls: int
    context_fields: tuple[str, ...] = ()
    state_update_fields: frozenset[str] = frozenset()
    legacy_runner: bool = True
    background_safe: bool = False


def default_subagents() -> dict[str, SubagentDefinition]:
    return {
        "repoAgent": SubagentDefinition(
            "repoAgent",
            "Explore local code and tests read-only",
            run_repo_agent,
            frozenset({"FileReadTool", "GrepTool", "FileGlobTool"}),
            8,
            context_fields=("latest_user_corrections",),
            state_update_fields=frozenset({"repo_context", "repo_explored"}),
        ),
        "searchAgent": SubagentDefinition(
            "searchAgent",
            "Research using web search",
            run_search_agent,
            frozenset({"WebSearchTool"}),
            4,
            context_fields=("research_notes", "sources"),
            state_update_fields=frozenset({"research_notes", "sources"}),
            background_safe=True,
        ),
        "codeAgent": SubagentDefinition(
            "codeAgent",
            "Implement and verify workspace changes",
            run_code_agent,
            frozenset(
                {
                    "FileReadTool",
                    "FileWriteTool",
                    "FileEditTool",
                    "GrepTool",
                    "FileGlobTool",
                    "BashTool",
                    "NotepadReadTool",
                    "NotepadAppendTool",
                    "LoadSkillTool",
                    "TodoUpdateTool",
                    "MCP_*",
                    "ToolSearchTool",
                }
            ),
            10,
            context_fields=(
                "repo_context",
                "repo_explored",
                "research_notes",
                "sources",
                "todos",
                "plan_summary",
                "acceptance_criteria",
                "verification_commands",
                "session_context",
                "latest_user_corrections",
            ),
            state_update_fields=frozenset({"todos", "code_agent_summary", "last_actor_summary"}),
        ),
    }


class SubagentRegistry:
    def __init__(self, definitions: dict[str, SubagentDefinition] | None = None) -> None:
        self._definitions = dict(definitions or {})

    def register(self, definition: SubagentDefinition, *, replace: bool = False) -> None:
        if not definition.name or not definition.allowed_tools or definition.max_tool_calls < 1:
            raise ValueError(
                "Subagent name, allowed_tools, and positive max_tool_calls are required"
            )
        if definition.name in self._definitions and not replace:
            raise ValueError(f"Subagent is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def unregister(self, name: str) -> None:
        self._definitions.pop(name, None)

    def get(self, name: str) -> SubagentDefinition | None:
        return self._definitions.get(name)

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def copy(self) -> SubagentRegistry:
        return SubagentRegistry(self._definitions)


_GLOBAL_REGISTRY = SubagentRegistry(default_subagents())


def register_subagent(definition: SubagentDefinition, *, replace: bool = False) -> None:
    """Register a Python Subagent for subsequently created runtimes."""
    _GLOBAL_REGISTRY.register(definition, replace=replace)


def registered_subagents() -> SubagentRegistry:
    return _GLOBAL_REGISTRY.copy()


def parse_subagent_configs(raw: str | None) -> dict[str, dict[str, Any]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MOKIO_SUBAGENTS must be a JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("MOKIO_SUBAGENTS must be a JSON object")  # noqa: TRY004 — preserve the public ValueError validation contract
    parsed: dict[str, dict[str, Any]] = {}
    for name, config in value.items():
        if not isinstance(config, dict):
            raise ValueError(f"Subagent config for {name} must be an object")  # noqa: TRY004 — preserve the public ValueError validation contract
        item: dict[str, Any] = {}
        if "max_tool_calls" in config:
            limit = int(config["max_tool_calls"])
            if not 1 <= limit <= 50:
                raise ValueError(f"Subagent max_tool_calls for {name} must be between 1 and 50")
            item["max_tool_calls"] = limit
        if "allowed_tools" in config:
            if not isinstance(config["allowed_tools"], list) or any(
                not isinstance(tool, str) or not tool for tool in config["allowed_tools"]
            ):
                raise ValueError(f"Subagent allowed_tools for {name} must be a list of names")
            item["allowed_tools"] = list(dict.fromkeys(config["allowed_tools"]))
        if "model_profile" in config:
            item["model_profile"] = str(config["model_profile"])
        parsed[str(name)] = item
    return parsed


def _normalise_evidence(values: Any, *, default_kind: str) -> list[EvidenceItem]:
    if not isinstance(values, list):
        return []
    evidence: list[EvidenceItem] = []
    for value in values:
        if isinstance(value, EvidenceItem):
            evidence.append(value)
        elif isinstance(value, str):
            evidence.append(EvidenceItem(default_kind, value, value, ""))
        elif isinstance(value, dict):
            label = str(value.get("label") or value.get("title") or value.get("name") or "evidence")
            nested_result = value.get("result")
            nested_result = nested_result if isinstance(nested_result, dict) else {}
            reference = str(
                value.get("reference")
                or value.get("url")
                or value.get("path")
                or nested_result.get("path")
                or nested_result.get("stdout_path")
                or ""
            )
            details = str(
                value.get("details")
                or value.get("snippet")
                or value.get("summary")
                or value.get("content")
                or nested_result.get("summary")
                or nested_result.get("error")
                or nested_result.get("stdout")
                or ""
            )
            kind = str(value.get("kind") or ("source" if value.get("url") else default_kind))
            evidence.append(EvidenceItem(kind, label, reference, details))
    return evidence


def _normalise_result(
    raw: SubagentResult | dict[str, Any],
    *,
    definition: SubagentDefinition,
    input_value: SubagentInput,
    legacy_state: dict[str, Any] | None = None,
    legacy_before: dict[str, Any] | None = None,
) -> SubagentResult:
    payload = raw.as_dict() if isinstance(raw, SubagentResult) else dict(raw or {})
    summary = str(payload.get("summary", ""))
    ok = bool(payload.get("ok", True))
    raw_unresolved = payload.get("unresolved", [])
    unresolved = (
        [str(item) for item in raw_unresolved]
        if isinstance(raw_unresolved, list)
        else ([str(raw_unresolved)] if raw_unresolved else [])
    )
    updates = payload.get("state_updates", {})
    if not isinstance(updates, dict):
        updates = {}

    # Adapt the existing search/code runner return shapes into explicit updates.
    context = dict(input_value.context)
    base_name = input_value.runtime.agent_profiles.get(definition.name, {}).get(
        "base", definition.name
    )
    if base_name == "searchAgent":
        sources = payload.get("sources", [])
        if isinstance(sources, list):
            updates = {
                **updates,
                "sources": _merge_sources(context.get("sources", []), sources),
                "research_notes": _append_note(str(context.get("research_notes", "")), summary),
            }
    elif base_name == "codeAgent":
        if "todos" in payload:
            updates = {
                **updates,
                "todos": payload["todos"],
                "code_agent_summary": summary,
                "last_actor_summary": summary,
            }

    # Legacy runners receive a projected copy. Only declared changed fields can
    # be returned to the coordinator as state updates.
    if legacy_state is not None and legacy_before is not None:
        for key in definition.state_update_fields:
            if key in legacy_state and legacy_state.get(key) != legacy_before.get(key):
                updates[key] = legacy_state[key]

    unexpected = set(updates) - definition.state_update_fields
    validated_updates = {
        key: copy.deepcopy(value)
        for key, value in updates.items()
        if key in definition.state_update_fields
    }
    if unexpected:
        return SubagentResult(
            ok=False,
            summary=summary,
            evidence=_normalise_evidence(
                payload.get("evidence", payload.get("sources", payload.get("tool_events", []))),
                default_kind="tool_event",
            ),
            unresolved=unresolved
            + [f"Runner returned undeclared state update(s): {', '.join(sorted(unexpected))}"],
            stop_reason="invalid_state_updates",
            state_updates=validated_updates,
            metadata=payload.get("metadata", {}),
        )

    evidence_values = payload.get("evidence")
    if evidence_values is None:
        evidence_values = payload.get(
            "sources" if definition.name == "searchAgent" else "tool_events", []
        )
    metadata = payload.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}
    if definition.name == "searchAgent" and "queries" in payload:
        metadata = {**metadata, "queries": payload["queries"]}
    return SubagentResult(
        ok=ok,
        summary=summary,
        evidence=_normalise_evidence(
            evidence_values,
            default_kind="source" if definition.name == "searchAgent" else "tool_event",
        ),
        unresolved=unresolved,
        state_updates=validated_updates,
        stop_reason=payload.get("stop_reason"),
        metadata=metadata,
    )


def _append_note(previous: str, addition: str) -> str:
    if not addition.strip():
        return previous
    if not previous.strip():
        return addition.strip()
    return previous.rstrip() + "\n\n" + addition.strip()


def _merge_sources(existing: Any, incoming: list[Any]) -> list[Any]:
    combined = list(existing) if isinstance(existing, list) else []
    seen = {
        str(item.get("url", "")) for item in combined if isinstance(item, dict) and item.get("url")
    }
    for item in incoming:
        key = str(item.get("url", "")) if isinstance(item, dict) else ""
        if not key or key not in seen:
            combined.append(item)
            if key:
                seen.add(key)
    return combined


def run_subagent(
    state: MokioGraphState,
    name: str,
    instruction: str,
    *,
    writer=None,
    handoff_id: str = "",
) -> dict[str, Any]:
    runtime = state["runtime"]
    check_run(runtime)
    registry = runtime.subagent_registry or _GLOBAL_REGISTRY
    definition = registry.get(name)
    if definition is None:
        return SubagentResult(
            ok=False,
            unresolved=[f"unknown Subagent: {name}"],
            stop_reason="unknown_subagent",
        ).as_dict()
    config = runtime.subagent_configs.get(name, {})
    configured_tools = set(config.get("allowed_tools", definition.allowed_tools))
    if not configured_tools.issubset(definition.allowed_tools):
        return SubagentResult(
            ok=False,
            unresolved=[f"Subagent {name} requested tools outside its maximum scope"],
            stop_reason="invalid_tool_scope",
        ).as_dict()

    try:
        limit = int(config.get("max_tool_calls", definition.max_tool_calls))
    except (TypeError, ValueError):
        return SubagentResult(
            ok=False,
            unresolved=[f"Subagent {name} has an invalid max_tool_calls setting"],
            stop_reason="invalid_budget",
        ).as_dict()
    if limit < 1 or limit > 50:
        return SubagentResult(
            ok=False,
            unresolved=[f"Subagent {name} max_tool_calls must be between 1 and 50"],
            stop_reason="invalid_budget",
        ).as_dict()
    context = {key: copy.deepcopy(state[key]) for key in definition.context_fields if key in state}
    input_value = SubagentInput(
        task=str(state.get("task", "")),
        instruction=instruction,
        runtime=runtime,
        context=MappingProxyType(context),
    )
    previous_agent_tools = runtime.active_agent_tools
    previous_skill_tools = runtime.active_skill_tools
    previous_skill_permissions = runtime.active_skill_permissions
    previous_skill_name = runtime.active_skill_name
    previous_skill_source = runtime.active_skill_source
    previous_budget = runtime.active_subagent_budget
    previous_model_profile = runtime.model_profile
    previous_prompt = runtime.active_agent_prompt
    previous_agent_name = runtime.active_agent_name

    def event_writer(event: dict[str, Any]) -> None:
        if writer is not None:
            annotated = {
                **event,
                "handoff_id": event.get("handoff_id") or handoff_id,
                "subagent": event.get("subagent") or name,
            }
            writer(annotated)
            # A pending stream-end keeps the producer's original dictionary.
            # Preserve its trace identity even if graph delivery is interrupted.
            for key in ("event_id", "trace_id", "trace_seq", "run_id", "context_version"):
                if key in annotated:
                    event[key] = annotated[key]

    budget = SubagentToolBudget(name, handoff_id, limit, event_writer)
    runtime.active_agent_tools = configured_tools
    runtime.active_subagent_budget = budget
    # A run-wide profile also governs synchronous children unless they explicitly
    # select another profile. Clearing it silently restores provider defaults.
    runtime.model_profile = config.get("model_profile") or previous_model_profile
    runtime.active_agent_prompt = runtime.agent_profiles.get(name, {}).get("system_prompt", "")
    runtime.active_agent_name = name
    legacy_state = None
    legacy_before = None
    try:
        if definition.legacy_runner:
            legacy_state = input_value.legacy_state()
            for key in definition.state_update_fields:
                if key in state:
                    legacy_state.setdefault(key, copy.deepcopy(state[key]))
            legacy_before = copy.deepcopy(
                {key: legacy_state.get(key) for key in definition.state_update_fields}
            )
            raw_result = definition.runner(
                legacy_state, instruction, writer=event_writer, max_loops=limit
            )
        else:
            raw_result = definition.runner(input_value, writer=event_writer, max_tool_calls=limit)
        result = _normalise_result(
            raw_result,
            definition=definition,
            input_value=input_value,
            legacy_state=legacy_state,
            legacy_before=legacy_before,
        )
    except (RunRedirected, RunBudgetExceeded, ModelGenerationStopped) as redirect:
        if legacy_state is not None:
            for key in definition.state_update_fields:
                if legacy_state.get(key) is not None:
                    redirect.state[key] = copy.deepcopy(legacy_state[key])
        raise
    except (RunInterrupted, ContextBudgetError):
        raise
    except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
        result = SubagentResult(
            ok=False,
            unresolved=[f"{type(exc).__name__}: {exc}"],
            stop_reason="runner_error",
        )
    finally:
        runtime.active_agent_tools = previous_agent_tools
        runtime.active_subagent_budget = previous_budget
        runtime.model_profile = previous_model_profile
        runtime.active_agent_prompt = previous_prompt
        runtime.active_agent_name = previous_agent_name
        runtime.active_skill_tools = previous_skill_tools
        runtime.active_skill_permissions = previous_skill_permissions
        runtime.active_skill_name = previous_skill_name
        runtime.active_skill_source = previous_skill_source

    if budget.exhausted:
        result.ok = False
        result.stop_reason = "cancelled" if budget.cancelled else "tool_budget_exhausted"
        message = (
            "Cancellation requested; Subagent stopped before its next tool call."
            if budget.cancelled
            else f"Tool-call budget exhausted ({budget.used}/{budget.limit}); task may be incomplete."
        )
        if message not in result.unresolved:
            result.unresolved.append(message)
    result.metadata = {
        **result.metadata,
        "tool_calls_used": budget.used,
        "tool_call_limit": budget.limit,
    }
    return {"agent": name, "handoff_id": handoff_id, **result.as_dict()}
