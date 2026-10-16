"""Optional TOML configuration; credentials remain environment references."""

from __future__ import annotations

import copy
import json
import os
import tomllib
from pathlib import Path
from typing import Any


def _merge(base: dict, update: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in update.items():
        result[key] = (
            _merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def load_settings(workspace: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in (
        Path.home() / ".mokioclaw/config.toml",
        workspace / ".mokioclaw/config.toml",
    ):
        if path.is_file():
            with path.open("rb") as handle:
                result = _merge(result, tomllib.load(handle))
    return result


def configure_runtime(runtime, *, explicit: dict | None = None) -> None:
    settings = load_settings(runtime.workspace)
    runtime.settings = settings
    # Configuration cannot replace live control objects or execution state.
    configurable = {
        "workflow_mode",
        "mcp_loading",
        "run_token_limit",
        "rewind_quota_bytes",
        "approval_mode",
        "checkpoint_mode",
        "trace_mode",
        "tool_policies",
        "bash_default_timeout_seconds",
        "bash_max_timeout_seconds",
        "bash_max_output_chars",
        "bash_env_file",
        "bash_deny_patterns",
        "bash_allow_patterns",
        "tool_retry_limit",
        "context_output_limit_chars",
        "context_result_limit_chars",
        "context_max_items",
        "context_max_depth",
        "context_max_nodes",
        "context_token_limit",
        "model_context_reserve_tokens",
        "context_safety_tokens",
        "context_high_ratio",
        "context_target_ratio",
        "mcp_servers",
        "subagent_configs",
        "mcp_timeout_seconds",
    }
    for name, value in settings.get("runtime", {}).items():
        if name not in configurable:
            raise ValueError(f"unknown runtime setting: {name}")
        setattr(runtime, name, value)
    for name in configurable:
        env = {
            "model_context_reserve_tokens": "MOKIO_MODEL_CONTEXT_RESERVE_TOKENS",
            "mcp_servers": "MOKIO_MCP_SERVERS",
            "subagent_configs": "MOKIO_SUBAGENTS",
        }.get(name, "MOKIO_" + name.upper())
        value = getattr(runtime, name)
        if env in os.environ:
            raw = os.environ[env]
            if name in {
                "mcp_servers",
                "subagent_configs",
                "tool_policies",
                "bash_deny_patterns",
                "bash_allow_patterns",
            }:
                value = json.loads(raw)
            elif name in {
                "context_high_ratio",
                "context_target_ratio",
                "mcp_timeout_seconds",
            }:
                value = float(raw)
            elif name in {
                "workflow_mode",
                "mcp_loading",
                "approval_mode",
                "checkpoint_mode",
                "trace_mode",
                "bash_env_file",
            }:
                value = raw
            else:
                value = int(raw)
        setattr(runtime, name, value)
    for name, value in (explicit or {}).items():
        if value is not None:
            setattr(runtime, name, value)
    if runtime.workflow_mode not in {"full", "light", "auto"}:
        raise ValueError("workflow_mode must be full, light, or auto")
    if runtime.mcp_loading not in {"deferred", "eager"}:
        raise ValueError("mcp_loading must be deferred or eager")
    runtime.run_token_limit = int(runtime.run_token_limit)
    if runtime.run_token_limit < 0:
        raise ValueError("run token limit must be nonnegative")
    runtime.rewind_quota_bytes = int(runtime.rewind_quota_bytes)
    if runtime.rewind_quota_bytes < 0:
        raise ValueError("rewind quota must be nonnegative")
    if runtime.bash_env_file is not None:
        runtime.bash_env_file = Path(runtime.bash_env_file)
    from mokioclaw.agents.registry import parse_subagent_configs
    from mokioclaw.core.tool_policy import parse_bash_patterns, parse_tool_policies
    from mokioclaw.tools.mcp_tools import parse_mcp_servers

    runtime.mcp_servers = parse_mcp_servers(json.dumps(runtime.mcp_servers))
    runtime.subagent_configs = parse_subagent_configs(json.dumps(runtime.subagent_configs))
    runtime.tool_policies = parse_tool_policies(json.dumps(runtime.tool_policies))
    for name in ("bash_deny_patterns", "bash_allow_patterns"):
        setattr(
            runtime,
            name,
            parse_bash_patterns(
                json.dumps(getattr(runtime, name)), variable="MOKIO_" + name.upper()
            ),
        )
    runtime.__post_init__()
