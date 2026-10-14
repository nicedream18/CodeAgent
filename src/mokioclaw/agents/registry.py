from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from mokioclaw.agents.code_agent import run_code_agent
from mokioclaw.agents.search_agent import run_search_agent
from mokioclaw.graph.state import MokioGraphState

Runner = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    runner: Runner
    allowed_tools: frozenset[str]
    max_tool_calls: int


def default_subagents() -> dict[str, SubagentDefinition]:
    return {
        "searchAgent": SubagentDefinition(
            "searchAgent",
            "Research using web search",
            run_search_agent,
            frozenset({"WebSearchTool"}),
            4,
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
                    "BashTool",
                    "NotepadReadTool",
                    "NotepadAppendTool",
                    "LoadSkillTool",
                    "TodoUpdateTool",
                    "MCP_*",
                }
            ),
            10,
        ),
    }


class SubagentRegistry:
    def __init__(
        self, definitions: dict[str, SubagentDefinition] | None = None
    ) -> None:
        self._definitions = dict(definitions or {})

    def register(
        self, definition: SubagentDefinition, *, replace: bool = False
    ) -> None:
        if (
            not definition.name
            or not definition.allowed_tools
            or definition.max_tool_calls < 1
        ):
            raise ValueError(
                "Subagent name, allowed_tools, and positive max_tool_calls are required"
            )
        if definition.name in self._definitions and not replace:
            raise ValueError(f"Subagent is already registered: {definition.name}")
        self._definitions[definition.name] = definition

    def get(self, name: str) -> SubagentDefinition | None:
        return self._definitions.get(name)

    def names(self) -> list[str]:
        return sorted(self._definitions)

    def copy(self) -> "SubagentRegistry":
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
        raise ValueError("MOKIO_SUBAGENTS must be a JSON object")
    parsed: dict[str, dict[str, Any]] = {}
    for name, config in value.items():
        if not isinstance(config, dict):
            raise ValueError(f"Subagent config for {name} must be an object")
        item: dict[str, Any] = {}
        if "max_tool_calls" in config:
            limit = int(config["max_tool_calls"])
            if not 1 <= limit <= 50:
                raise ValueError(
                    f"Subagent max_tool_calls for {name} must be between 1 and 50"
                )
            item["max_tool_calls"] = limit
        if "allowed_tools" in config:
            if not isinstance(config["allowed_tools"], list):
                raise ValueError(f"Subagent allowed_tools for {name} must be a list")
            item["allowed_tools"] = [str(tool) for tool in config["allowed_tools"]]
        parsed[str(name)] = item
    return parsed


def run_subagent(
    state: MokioGraphState,
    name: str,
    instruction: str,
    *,
    writer=None,
) -> dict[str, Any]:
    runtime = state["runtime"]
    registry = runtime.subagent_registry or _GLOBAL_REGISTRY
    definition = registry.get(name)
    if definition is None:
        return {
            "ok": False,
            "error": f"unknown Subagent: {name}",
            "summary": "",
            "evidence": [],
            "unresolved": [],
        }
    config = runtime.subagent_configs.get(name, {})
    configured_tools = set(config.get("allowed_tools", definition.allowed_tools))
    if not configured_tools.issubset(definition.allowed_tools):
        return {
            "ok": False,
            "error": f"Subagent {name} requested tools outside its maximum scope",
            "summary": "",
            "evidence": [],
            "unresolved": [],
        }
    previous_agent_tools = runtime.active_agent_tools
    previous_skill_tools = runtime.active_skill_tools
    previous_skill_permissions = runtime.active_skill_permissions
    previous_skill_name = runtime.active_skill_name
    previous_skill_source = runtime.active_skill_source
    runtime.active_agent_tools = configured_tools
    try:
        limit = int(config.get("max_tool_calls", definition.max_tool_calls))
        result = definition.runner(state, instruction, writer=writer, max_loops=limit)
    finally:
        runtime.active_agent_tools = previous_agent_tools
        if name == "codeAgent":
            runtime.active_skill_tools = previous_skill_tools
            runtime.active_skill_permissions = previous_skill_permissions
            runtime.active_skill_name = previous_skill_name
            runtime.active_skill_source = previous_skill_source
    summary = str(result.get("summary", ""))
    return {
        **result,
        "agent": name,
        "summary": summary,
        "evidence": result.get(
            "evidence",
            result.get("sources", [])
            if name == "searchAgent"
            else result.get("tool_events", []),
        ),
        "unresolved": result.get(
            "unresolved",
            []
            if result.get("ok")
            else [str(result.get("error", "Subagent did not complete"))],
        ),
    }
