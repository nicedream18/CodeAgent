from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import StructuredTool

from mokioclaw.core.state import RuntimeState

VALID_SKILL_PERMISSIONS = {
    "filesystem_read",
    "filesystem_write",
    "shell",
    "network",
    "mcp",
}


class SkillDiagnostic(ValueError):
    """A user-facing, serializable Skill format or registration diagnostic."""

    def __init__(self, code: str, message: str, path: Path, name: str = "") -> None:
        self.diagnostic = {
            "code": code,
            "message": message,
            "skill_name": name or path.parent.name,
            "path": str(path),
        }
        super().__init__(f"{code}: {message} ({path})")


def skill_roots(runtime: RuntimeState) -> list[Path]:
    roots = [
        runtime.workspace / ".mokioclaw" / "skills",
        Path.home() / ".mokioclaw" / "skills",
    ]
    return list(dict.fromkeys(root.resolve() for root in roots))


def _registered_tool_names(runtime: RuntimeState) -> set[str]:
    from mokioclaw.tools.registry import TOOL_SPECS, build_tools

    names = {spec.name for spec in TOOL_SPECS}
    # Tool factories include configured MCP tools, whose names are runtime-specific.
    names.update(tool.name for tool in build_tools(runtime))
    return names


def discover_skills(runtime: RuntimeState) -> list[dict[str, Any]]:
    """Discover Skills with workspace shadowing, including invalid shadow files."""
    registered = _registered_tool_names(runtime)
    candidates: list[tuple[Path, dict[str, Any] | None, dict[str, Any] | None]] = []
    for root in skill_roots(runtime):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                skill = parse_skill(path, registered_tools=registered)
                candidates.append((path, skill, None))
            except SkillDiagnostic as exc:
                candidates.append((path, None, exc.diagnostic))
            except OSError as exc:
                diagnostic = {
                    "code": "read_error",
                    "message": str(exc),
                    "skill_name": path.parent.name,
                    "path": str(path),
                }
                candidates.append((path, None, diagnostic))

    # Prefer frontmatter identity when available; invalid files use the
    # directory name unless the diagnostic already extracted a declared name.
    seen_dirs: set[str] = set()
    result: list[dict[str, Any]] = []
    for path, skill, diagnostic in candidates:
        identity = (
            skill["name"]
            if skill is not None
            else diagnostic.get("skill_name", path.parent.name)
        )
        if identity in seen_dirs:
            continue
        seen_dirs.add(identity)
        if skill is None:
            result.append(
                {
                    "name": identity,
                    "description": f"Invalid Skill: {diagnostic['message']}",
                    "valid": False,
                    "path": str(path),
                    "source": _skill_source(runtime, path),
                    "diagnostic": diagnostic,
                }
            )
        else:
            skill["source"] = _skill_source(runtime, path)
            result.append(skill)
    return result


def parse_skill(
    path: Path, *, registered_tools: set[str] | None = None
) -> dict[str, Any]:
    from mokioclaw.tools.registry import TOOL_SPECS, tool_spec_for

    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillDiagnostic("read_error", str(exc), path) from exc
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", content, re.DOTALL)
    if not match:
        raise SkillDiagnostic(
            "missing_frontmatter", "expected YAML frontmatter between --- lines", path
        )
    metadata_text, body = match.groups()
    if not metadata_text.strip():
        raise SkillDiagnostic("missing_frontmatter", "YAML frontmatter is empty", path)
    try:
        metadata = yaml.safe_load(metadata_text)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = (
            f"line {mark.line + 2}, column {mark.column + 1}" if mark else "frontmatter"
        )
        raise SkillDiagnostic("invalid_yaml", f"{location}: {exc}", path) from exc
    if not isinstance(metadata, dict):
        raise SkillDiagnostic(
            "invalid_frontmatter", "frontmatter must be a YAML mapping", path
        )

    name_value = metadata.get("name")
    name = name_value.strip() if isinstance(name_value, str) else path.parent.name
    for required in ("name", "description"):
        if required not in metadata:
            raise SkillDiagnostic(
                "missing_field", f"required field '{required}' is missing", path, name
            )
    if not isinstance(name_value, str) or not name_value.strip():
        raise SkillDiagnostic(
            "invalid_field_type", "'name' must be a non-empty string", path, name
        )
    description_value = metadata["description"]
    if not isinstance(description_value, str) or not description_value.strip():
        raise SkillDiagnostic(
            "invalid_field_type", "'description' must be a non-empty string", path, name
        )
    if not body.strip():
        raise SkillDiagnostic(
            "missing_body", "Skill Markdown body must not be empty", path, name
        )

    tools = _string_list(metadata, "tools", path, name)
    permissions = _string_list(metadata, "permissions", path, name)
    unknown_permissions = sorted(set(permissions) - VALID_SKILL_PERMISSIONS)
    if unknown_permissions:
        raise SkillDiagnostic(
            "unknown_permission",
            f"unknown permission(s): {', '.join(unknown_permissions)}",
            path,
            name,
        )

    known_tools = (
        registered_tools
        if registered_tools is not None
        else {spec.name for spec in TOOL_SPECS}
    )
    for tool_name in tools:
        # MCP tools are valid only when present in this runtime's discovered set.
        if tool_name not in known_tools:
            raise SkillDiagnostic(
                "unknown_tool", f"tool is not registered: {tool_name}", path, name
            )
        spec = tool_spec_for(tool_name)
        if spec is None:
            raise SkillDiagnostic(
                "unknown_tool", f"tool is not registered: {tool_name}", path, name
            )
        missing = sorted(spec.capabilities - set(permissions))
        if missing:
            raise SkillDiagnostic(
                "missing_tool_permission",
                f"tool '{tool_name}' requires permission(s): {', '.join(missing)}",
                path,
                name,
            )

    return {
        "name": name,
        "description": description_value.strip(),
        "tools": tools,
        "permissions": permissions,
        "body": body.strip(),
        "path": str(path),
        "valid": True,
    }


def _string_list(
    metadata: dict[str, Any], key: str, path: Path, name: str
) -> list[str]:
    value = metadata.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise SkillDiagnostic(
            "invalid_field_type", f"'{key}' must be a list of strings", path, name
        )
    normalized = [item.strip() for item in value]
    if any(not item for item in normalized):
        raise SkillDiagnostic(
            "invalid_field_value", f"'{key}' cannot contain empty items", path, name
        )
    return list(dict.fromkeys(normalized))


def _skill_source(runtime: RuntimeState, path: Path) -> str:
    return "workspace" if path.is_relative_to(runtime.workspace.resolve()) else "user"


def load_skill(runtime: RuntimeState, name: str) -> dict[str, Any]:
    discovered = discover_skills(runtime)
    skill = next((item for item in discovered if item["name"] == name), None)
    if skill is None:
        diagnostic = {
            "code": "not_found",
            "message": f"Skill not found: {name}",
            "skill_name": name,
            "path": "",
        }
        return {
            "ok": False,
            "error": diagnostic["message"],
            "error_type": "invalid_input",
            "retryable": False,
            "diagnostic": diagnostic,
        }
    if not skill.get("valid"):
        diagnostic = skill["diagnostic"]
        return {
            "ok": False,
            "error": diagnostic["message"],
            "error_type": "invalid_input",
            "retryable": False,
            "diagnostic": diagnostic,
        }

    runtime.active_skill_tools = set(skill["tools"])
    runtime.active_skill_permissions = set(skill["permissions"])
    runtime.active_skill_name = skill["name"]
    runtime.active_skill_source = skill["source"]
    # Skill instructions are returned only to the calling model, not emitted to trace.
    return {"ok": True, **skill}


def skill_summaries(runtime: RuntimeState) -> str:
    rows = []
    for item in discover_skills(runtime):
        if item.get("valid"):
            rows.append(f"- {item['name']}: {item['description']}")
        else:
            diagnostic = item["diagnostic"]
            rows.append(
                f"- {item['name']} [invalid: {diagnostic['code']}]: "
                f"{diagnostic['message']} ({diagnostic['path']})"
            )
    return "\n".join(rows) if rows else "(no Skills discovered)"


def build_load_skill_tool(runtime: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="LoadSkillTool",
        func=lambda name: load_skill(runtime, name),
        description="Load one discovered SKILL.md. Args: name. Its declared tool and permission scope applies to subsequent tool calls.",
    )
