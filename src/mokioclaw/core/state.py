from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

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
class RuntimeState:
    workspace: Path
    read_files: dict[Path, FileSnapshot] = field(default_factory=dict)
    approval_mode: str = "inline"
    approval_handler: Callable[[ApprovalRequest], ApprovalDecision | bool] | None = None
    bash_default_timeout_seconds: int = 120
    bash_max_timeout_seconds: int = 600
    bash_max_output_chars: int = 6000
    bash_env_file: Path | None = None
    checkpoint_mode: str = "light"
    resume_from: Path | None = None
    trace_mode: str = "on"
    trace_id: str | None = None
    tool_policies: dict[str, str] = field(default_factory=dict)
    bash_deny_patterns: list[str] = field(default_factory=list)
    bash_allow_patterns: list[str] = field(default_factory=list)
    tool_retry_limit: int = 2
    context_output_limit_chars: int = 12000
    model_context_reserve_tokens: int = 8192
    active_skill_tools: set[str] | None = None
    active_skill_permissions: set[str] | None = None
    active_skill_name: str | None = None
    active_skill_source: str | None = None
    active_agent_tools: set[str] | None = None
    subagent_configs: dict[str, dict[str, Any]] = field(default_factory=dict)
    subagent_registry: Any = None
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    mcp_timeout_seconds: float = 30.0
    tool_cache: dict[str, list[Any]] = field(default_factory=dict)
    mcp_discovery_errors: list[dict[str, str]] = field(default_factory=list)

    def __post_init__(self) -> None:
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
