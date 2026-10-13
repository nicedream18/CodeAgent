from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool

from mokioclaw.core.state import RuntimeState


VALID_SKILL_PERMISSIONS = {
    "filesystem_read",
    "filesystem_write",
    "shell",
    "network",
    "mcp",
}


def skill_roots(runtime: RuntimeState) -> list[Path]:
    roots = [
        runtime.workspace / ".mokioclaw" / "skills",
        Path.home() / ".mokioclaw" / "skills",
    ]
    return list(dict.fromkeys(root.resolve() for root in roots))


def discover_skills(runtime: RuntimeState) -> list[dict[str, Any]]:
    skills: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in skill_roots(runtime):
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                skill = parse_skill(path)
            except (OSError, ValueError) as exc:
                skills.append(
                    {
                        "name": path.parent.name,
                        "description": f"Invalid skill: {exc}",
                        "valid": False,
                    }
                )
                continue
            if skill["name"] not in seen:
                seen.add(skill["name"])
                skills.append(skill)
    return skills


def parse_skill(path: Path) -> dict[str, Any]:
    content = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", content, re.DOTALL)
    if not match:
        raise ValueError("expected YAML-style frontmatter between --- lines")
    metadata_text, body = match.groups()
    metadata: dict[str, Any] = {}
    current_list: str | None = None
    for line in metadata_text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith("  - ") and current_list:
            metadata[current_list].append(line[4:].strip().strip("\"'"))
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise ValueError(f"invalid frontmatter line: {line}")
        key = key.strip()
        value = value.strip()
        if not value:
            metadata[key] = []
            current_list = key
        else:
            metadata[key] = value.strip("\"'")
            current_list = None
    name = str(metadata.get("name") or path.parent.name).strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description or not body.strip():
        raise ValueError("name, description, and a non-empty body are required")
    tools = _as_string_list(metadata.get("tools", []))
    permissions = _as_string_list(metadata.get("permissions", []))
    unknown_permissions = sorted(set(permissions) - VALID_SKILL_PERMISSIONS)
    if unknown_permissions:
        raise ValueError(f"unknown permission(s): {', '.join(unknown_permissions)}")
    return {
        "name": name,
        "description": description,
        "tools": tools,
        "permissions": permissions,
        "body": body.strip(),
        "path": str(path),
        "valid": True,
    }


def load_skill(runtime: RuntimeState, name: str) -> dict[str, Any]:
    skill = next(
        (
            item
            for item in discover_skills(runtime)
            if item.get("valid") and item["name"] == name
        ),
        None,
    )
    if skill is None:
        return {
            "ok": False,
            "error": f"skill not found or invalid: {name}",
            "error_type": "invalid_input",
            "retryable": False,
        }
    runtime.active_skill_tools = set(skill["tools"]) if skill["tools"] else None
    runtime.active_skill_permissions = (
        set(skill["permissions"]) if skill["permissions"] else None
    )
    return {"ok": True, **skill}


def skill_summaries(runtime: RuntimeState) -> str:
    rows = [
        f"- {item['name']}: {item['description']}" for item in discover_skills(runtime)
    ]
    return "\n".join(rows) if rows else "(no Skills discovered)"


def build_load_skill_tool(runtime: RuntimeState) -> StructuredTool:
    return StructuredTool.from_function(
        name="LoadSkillTool",
        func=lambda name: load_skill(runtime, name),
        description="Load one discovered SKILL.md. Args: name. Its declared tool and permission scope applies to subsequent tool calls.",
    )


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []
