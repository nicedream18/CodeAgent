from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Callable, Literal

from langchain_core.tools import BaseTool, StructuredTool

from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.bash_tool import bash_tool_description, run_bash
from mokioclaw.tools.file_tools import edit_file, read_file, write_file
from mokioclaw.tools.grep_tool import grep
from mokioclaw.tools.mcp_tools import build_mcp_tools
from mokioclaw.tools.notepad_tool import append_notepad, read_notepad
from mokioclaw.tools.skills import build_load_skill_tool
from mokioclaw.tools.web_search_tool import build_web_search_tool

ToolFactory = Callable[[RuntimeState], BaseTool]
ToolProfile = Literal["execution", "read_only"]


@dataclass(frozen=True)
class ToolSpec:
    """Runtime metadata for a tool, shared by discovery and execution policy."""

    name: str
    factory: ToolFactory | None
    profiles: frozenset[ToolProfile]
    description: str
    capabilities: frozenset[str] = frozenset()
    default_policy: str | None = None
    idempotent: bool = False
    read_only: bool = False
    input_schema: dict[str, Any] | None = None


def _file_read(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="FileReadTool",
        func=lambda file_path, offset=0, limit=2000, char_offset=None, char_limit=8000: (
            read_file(state, file_path, offset, limit, char_offset, char_limit)
        ),
        description="Read a UTF-8 text file inside the workspace. Use offset/limit for lines, or char_offset/char_limit for a long single line or a saved tool result. Returned next_char_offset supports further reading.",
    )


def _file_write(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="FileWriteTool",
        func=lambda file_path, content: write_file(state, file_path, content),
        description="Create a new file or rewrite an existing file inside the workspace.",
    )


def _file_edit(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="FileEditTool",
        func=lambda file_path, old_text, new_text: edit_file(
            state, file_path, old_text, new_text
        ),
        description="Edit an existing workspace file by replacing one unique old_text snippet.",
    )


def _grep(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="GrepTool",
        func=lambda pattern, path=".", glob=None, head_limit=50, ignore_case=False: (
            grep(state, pattern, path, glob, head_limit, ignore_case)
        ),
        description="Search workspace text files by regex pattern and return matching lines.",
    )


def _bash(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="BashTool",
        func=lambda command, timeout_seconds=None, run_in_background=False: run_bash(
            state, command, timeout_seconds, run_in_background
        ),
        description=bash_tool_description(),
    )


def _notepad_read(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="NotepadReadTool",
        func=lambda: read_notepad(state),
        description="Read the durable workspace notepad from NOTEPAD.md.",
    )


def _notepad_append(state: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="NotepadAppendTool",
        func=lambda heading, content: append_notepad(state, heading, content),
        description="Append a durable markdown note to NOTEPAD.md. Args: heading, content.",
    )


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        "FileReadTool",
        _file_read,
        frozenset({"execution", "read_only"}),
        "Read a workspace file",
        frozenset({"filesystem_read"}),
        idempotent=True,
        read_only=True,
    ),
    ToolSpec(
        "FileWriteTool",
        _file_write,
        frozenset({"execution"}),
        "Write a workspace file",
        frozenset({"filesystem_write"}),
    ),
    ToolSpec(
        "FileEditTool",
        _file_edit,
        frozenset({"execution"}),
        "Edit a workspace file",
        frozenset({"filesystem_write"}),
    ),
    ToolSpec(
        "GrepTool",
        _grep,
        frozenset({"execution", "read_only"}),
        "Search workspace files",
        frozenset({"filesystem_read"}),
        idempotent=True,
        read_only=True,
    ),
    ToolSpec(
        "BashTool",
        _bash,
        frozenset({"execution", "read_only"}),
        "Run a workspace shell command",
        frozenset({"shell"}),
    ),
    ToolSpec(
        "NotepadReadTool",
        _notepad_read,
        frozenset({"execution", "read_only"}),
        "Read the workspace notepad",
        frozenset({"filesystem_read"}),
        idempotent=True,
        read_only=True,
    ),
    ToolSpec(
        "NotepadAppendTool",
        _notepad_append,
        frozenset({"execution"}),
        "Append to the workspace notepad",
        frozenset({"filesystem_write"}),
    ),
    ToolSpec(
        "LoadSkillTool",
        build_load_skill_tool,
        frozenset({"execution"}),
        "Load a discovered Skill",
    ),
    ToolSpec(
        "WebSearchTool",
        lambda _state: build_web_search_tool(),
        frozenset({"read_only"}),
        "Search the web",
        frozenset({"network"}),
        idempotent=True,
        read_only=True,
    ),
    # TodoUpdateTool is constructed per CodeAgent invocation because its tool
    # function closes over that invocation's TODO list.
    ToolSpec("TodoUpdateTool", None, frozenset(), "Update a CodeAgent todo"),
    # Planner orchestration tools are constructed from graph state, but their
    # authorization metadata still lives in this registry.
    ToolSpec("TodoWriteTool", None, frozenset(), "Publish planner TODOs"),
    ToolSpec(
        "CallSubagentTool", None, frozenset(), "Delegate to a registered Subagent"
    ),
    ToolSpec(
        "CallSearchAgentTool", None, frozenset(), "Delegate research to searchAgent"
    ),
    ToolSpec(
        "CallCodeAgentTool", None, frozenset(), "Delegate implementation to codeAgent"
    ),
    ToolSpec("AgentResultTool", None, frozenset(), "Read a background Subagent result"),
    ToolSpec("AgentCancelTool", None, frozenset(), "Cancel a background Subagent"),
)


class ToolRegistry:
    """A per-runtime tool registry with profile-based schema projection."""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}
        self._specs: dict[str, ToolSpec] = {}

    def register(self, tool: BaseTool, spec: ToolSpec) -> None:
        if tool.name != spec.name:
            raise ValueError(
                f"tool name {tool.name!r} does not match spec {spec.name!r}"
            )
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool registration: {tool.name}")
        schema = getattr(tool, "args_schema", None)
        if schema is not None:
            if hasattr(schema, "model_json_schema"):
                input_schema = schema.model_json_schema()
            elif hasattr(schema, "schema"):
                input_schema = schema.schema()
            else:
                input_schema = None
            if input_schema is not None:
                spec = replace(spec, input_schema=input_schema)
        self._tools[tool.name] = tool
        self._specs[tool.name] = spec

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def spec(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def tools(self, profile: ToolProfile) -> list[BaseTool]:
        return [
            tool
            for name, tool in self._tools.items()
            if profile in self._specs[name].profiles
        ]

    def tool_schemas(self, profile: ToolProfile) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description or tool.description,
                "input_schema": spec.input_schema or {},
            }
            for name, tool in self._tools.items()
            if profile in (spec := self._specs[name]).profiles
        ]


def tool_spec_for(name: str) -> ToolSpec | None:
    for spec in TOOL_SPECS:
        if spec.name == name:
            return spec
    if name.startswith("MCP_"):
        return ToolSpec(
            name,
            None,
            frozenset({"execution"}),
            "MCP server tool",
            frozenset({"mcp"}),
            "ask",
        )
    return None


def _build_registry(state: RuntimeState) -> ToolRegistry:
    registry = ToolRegistry()
    for spec in TOOL_SPECS:
        if spec.factory is not None:
            registry.register(spec.factory(state), spec)
    for tool in build_mcp_tools(state):
        spec = tool_spec_for(tool.name)
        assert spec is not None
        registry.register(tool, spec)
    return registry


def build_tools(state: RuntimeState) -> list[BaseTool]:
    """Compatibility builder for execution agents."""
    if "write" not in state.tool_cache:
        state.tool_cache["write"] = _build_registry(state).tools("execution")
    return state.tool_cache["write"]


def build_read_only_tools(state: RuntimeState) -> list[BaseTool]:
    """Compatibility builder for read-only agents."""
    if "read_only" not in state.tool_cache:
        state.tool_cache["read_only"] = _build_registry(state).tools("read_only")
    return state.tool_cache["read_only"]


def build_tool(state: RuntimeState, name: str) -> BaseTool | None:
    """Build and return one tool by name, preserving the same registry metadata."""
    for spec in TOOL_SPECS:
        if spec.name == name and spec.factory is not None:
            return spec.factory(state)
    for tools in (build_tools(state), build_read_only_tools(state)):
        for tool in tools:
            if tool.name == name:
                return tool
    return None
