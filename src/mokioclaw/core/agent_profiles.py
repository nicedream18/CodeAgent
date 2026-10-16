"""Data-only role definitions, constrained by an existing Python runner."""

from __future__ import annotations

import copy
import hashlib
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class AgentProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    description: str
    base: Literal["repoAgent", "codeAgent", "searchAgent"]
    system_prompt: str
    allowed_tools: list[str]
    max_tool_calls: int = Field(default=8, ge=1, le=50)
    model_profile: str | None = None


def load_agent_profiles(runtime: Any) -> None:
    registry = runtime.subagent_registry
    originals = registry.copy()
    metadata: dict[str, dict[str, Any]] = {}
    diagnostics: list[dict[str, str]] = []
    for directory in [Path.home() / ".mokioclaw/agents", runtime.workspace / ".mokioclaw/agents"]:
        seen: set[str] = set()
        for path in sorted(directory.glob("*.toml")):
            rejected = path.stem
            try:
                source = path.read_bytes()
                raw = tomllib.loads(source.decode("utf-8"))
                data = raw.get("agent", raw)
                if not isinstance(data, dict):
                    raise ValueError("agent must be a TOML table")
                rejected = str(data.get("name", path.stem))
                profile = AgentProfile.model_validate({"name": path.stem, **data})
                if profile.name in seen:
                    raise ValueError("duplicate role name within the same source")
                seen.add(profile.name)
                base = originals.get(profile.base)
                if base is None:
                    raise ValueError("base runner unavailable")
                for tool in profile.allowed_tools:
                    if tool not in base.allowed_tools and not (
                        tool.startswith("MCP_") and "MCP_*" in base.allowed_tools
                    ):
                        raise ValueError(f"tool exceeds base capability: {tool}")
                if (
                    profile.name in {"repoAgent", "codeAgent", "searchAgent"}
                    and profile.base != profile.name
                ):
                    raise ValueError("built-in role base cannot be changed")
                if profile.model_profile and profile.model_profile not in runtime.settings.get(
                    "model_profiles", {}
                ):
                    raise ValueError("unknown model profile")
                definition = replace(
                    base,
                    name=profile.name,
                    description=profile.description,
                    allowed_tools=frozenset(profile.allowed_tools),
                    max_tool_calls=profile.max_tool_calls,
                )
                registry.register(definition, replace=True)
                # An explicit env/project subagent override may narrow this profile further.
                config = {
                    "max_tool_calls": profile.max_tool_calls,
                    "model_profile": profile.model_profile,
                }
                config.update(runtime.subagent_configs.get(profile.name, {}))
                runtime.subagent_configs[profile.name] = config
                metadata[profile.name] = {
                    **profile.model_dump(),
                    "source": str(path),
                    "version": hashlib.sha256(source).hexdigest(),
                }
            except (ValueError, OSError, ValidationError) as exc:
                diagnostics.append({"source": str(path), "error": str(exc)})
                # Invalid higher-priority files shadow, rather than silently load a lower role.
                registry.unregister(rejected)
                metadata.pop(rejected, None)
    runtime.agent_profiles = copy.deepcopy(metadata)
    runtime.agent_profile_diagnostics = diagnostics
