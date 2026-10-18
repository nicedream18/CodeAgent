from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mokioclaw.core.approval import (
    ApprovalDecision,
    ApprovalRequest,
    normalize_approval_mode,
)
from mokioclaw.core.checkpoint import normalize_checkpoint_mode
from mokioclaw.core.trace import normalize_trace_mode


@dataclass(frozen=True)
class FileSnapshot:
    path: Path
    mtime_ns: int
    complete: bool


@dataclass
class SubagentToolBudget:
    subagent_name: str
    handoff_id: str
    limit: int
    writer: Callable[[dict], None] | None = None
    used: int = 0
    exhausted: bool = False
    exhaustion_event_emitted: bool = False
    cancelled: bool = False

    def reserve(self) -> bool:
        if self.used >= self.limit:
            self.exhausted = True
            return False
        self.used += 1
        return True


@dataclass(kw_only=True)
class RuntimeConfig:
    """Configuration defaults; mutable execution fields live in RuntimeState."""

    settings: dict[str, Any] = field(default_factory=dict)
    execution_backend: str = "local"
    docker_image: str = "mokioclaw-execution:0.2.0"
    workflow_mode: str = "full"
    mcp_loading: str = "deferred"
    run_token_limit: int = 0
    run_request_limit: int = 0
    run_tool_limit: int = 0
    evaluation_mode: bool = False
    rewind_quota_bytes: int = 64 * 1024 * 1024
    approval_mode: str = "inline"
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None
    checkpoint_mode: str = "light"
    trace_mode: str = "on"
    tool_policies: dict[str, str] = field(default_factory=dict)
    bash_deny_patterns: list[str] = field(default_factory=list)
    bash_allow_patterns: list[str] = field(default_factory=list)
    tool_retry_limit: int = 2
    context_output_limit_chars: int = 12000
    context_result_limit_chars: int = 24000
    context_max_items: int = 100
    context_max_depth: int = 8
    context_max_nodes: int = 1000
    context_token_limit: int | None = None
    context_compression_enabled: bool = True
    model_context_reserve_tokens: int = 8192
    context_safety_tokens: int = 1024
    context_high_ratio: float = 0.85
    context_target_ratio: float = 0.60
    subagent_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    mcp_timeout_seconds: float = 30.0


@dataclass
class RuntimeState(RuntimeConfig):
    workspace: Path
    execution_image_digest: str = ""
    light_active: bool = False
    light_target: str | None = None
    light_escalation: bool = False
    mcp_loaded_tools: dict[str, Any] = field(default_factory=dict)
    mcp_catalog_version: int = 0
    mcp_config_digest: str = ""
    instructions: Any = None
    instructions_reported: int = -1
    context_dirty: bool = False
    run_id: str = ""
    session_id: str = ""
    model_metrics: dict[str, Any] = field(default_factory=dict)
    model_profile: str | None = None
    agent_profiles: dict[str, Any] = field(default_factory=dict)
    agent_profile_diagnostics: list[dict[str, str]] = field(default_factory=list)
    active_agent_prompt: str = ""
    active_agent_name: str | None = None
    explicit_skill: str | None = None
    foreground_state: dict[str, Any] | None = None
    completion_budget: Any = None
    hook_context: list[str] = field(default_factory=list)
    tool_execution_count: int = 0
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)
    resume_from: Path | None = None
    trace_id: str | None = None
    context_control: Any = None
    context_version: int = 0
    context_request_sequence: int = 0
    context_replacements: dict[str, list[Any]] = field(default_factory=dict)
    context_checkpoint: dict[str, Any] = field(default_factory=dict)
    active_skill_tools: set[str] | None = None
    active_skill_permissions: set[str] | None = None
    active_skill_name: str | None = None
    active_skill_source: str | None = None
    active_agent_tools: set[str] | None = None
    active_subagent_budget: SubagentToolBudget | None = None
    subagent_registry: Any = None
    tool_cache: dict[str, list[Any]] = field(default_factory=dict)
    mcp_discovery_errors: list[dict[str, str]] = field(default_factory=list)
    subagent_task_manager: Any = None
    parent_run_id: str | None = None
    subagent_parent_writer: Any = None
    subagent_cancel_event: Any = None
    run_control: Any = None
    mcp_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    subagent_applied_results: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        if not isinstance(self.context_compression_enabled, bool):
            raise ValueError("context_compression_enabled must be a boolean")
        if self.execution_backend not in {"local", "docker"}:
            raise ValueError("execution_backend must be local or docker")
        if self.mcp_timeout_seconds <= 0:
            raise ValueError("MCP timeout must be positive")
        if (
            self.context_result_limit_chars < 1024
            or min(
                self.context_output_limit_chars,
                self.context_max_items,
                self.context_max_depth,
                self.context_max_nodes,
            )
            <= 0
        ):
            raise ValueError(
                "invalid tool result budget: total must be at least 1024 and all limits positive"
            )
        self.approval_mode = normalize_approval_mode(self.approval_mode)
        self.checkpoint_mode = normalize_checkpoint_mode(self.checkpoint_mode)
        self.trace_mode = normalize_trace_mode(self.trace_mode)

    def record_read(self, path: Path, *, complete: bool) -> None:
        stat = path.stat()
        resolved = path.resolve()
        self.read_files[resolved] = FileSnapshot(
            path=resolved,
            mtime_ns=stat.st_mtime_ns,
            complete=complete,
        )

    def snapshot_for(self, path: Path) -> FileSnapshot | None:
        return self.read_files.get(path.resolve())

    def assert_workspace_path(self, path: Path) -> Path:
        resolved = path.resolve()
        workspace = self.workspace.resolve()
        if resolved != workspace and workspace not in resolved.parents:
            raise ValueError(f"path must stay inside workspace: {workspace}")
        return resolved
