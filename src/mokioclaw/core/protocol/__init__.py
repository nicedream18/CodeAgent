"""Version one wire contracts, shared by the daemon, clients and documentation."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

PROTOCOL_VERSION = 1
CAPABILITIES = ["typed_protocol", "explicit_skill", "approval_memory", "layered_trace"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)


class RpcRequest(Contract):
    jsonrpc: Literal["2.0"] = "2.0"
    id: StrictStr | StrictInt
    method: StrictStr
    params: dict[str, Any] = Field(default_factory=dict)
    protocol_version: Literal[1] = 1


class RpcError(Contract):
    code: int
    message: str


class RpcResponse(Contract):
    jsonrpc: Literal["2.0"] = "2.0"
    id: StrictStr | StrictInt | None = None
    result: Any = None
    error: RpcError | None = None


class RunOptions(Contract):
    model_config = ConfigDict(extra="forbid", strict=True)
    task: str | None = None
    workspace: str | None = None
    session_workspace: str | None = None
    resume_workspace: str | None = None
    max_attempts: int = Field(default=3, ge=1)
    approval_mode: Literal["inline", "auto", "deny"] | None = None
    checkpoint_mode: Literal["light", "strict", "off"] | None = None
    trace_mode: Literal["on", "off"] | None = None
    workflow_mode: Literal["full", "light", "auto"] | None = None
    skill: str | None = None


class Params(Contract):
    token: str = ""


class StartRun(Params):
    mode: Literal["run", "session"] = "run"
    options: RunOptions = Field(default_factory=RunOptions)

    @model_validator(mode="after")
    def validate_task(self) -> StartRun:
        if (
            not (self.options.task and self.options.task.strip())
            and not self.options.resume_workspace
        ):
            raise ValueError("task or resume_workspace is required")
        return self


class RunId(Params):
    run_id: StrictStr


class Subscribe(RunId):
    after_seq: StrictInt = Field(default=0, ge=0)


class SubmitMessage(RunId):
    message_id: StrictStr = Field(min_length=1, max_length=128)
    text: StrictStr = Field(min_length=1, max_length=8192)


class CompactId(RunId):
    request_id: StrictStr


class ApprovalResponse(RunId):
    approval_id: StrictStr
    tool_call_id: str = ""
    approved: StrictBool
    reason: str = ""
    scope: Literal["once", "session", "project"] = "once"
    match_scope: Literal["exact", "directory"] = "exact"
    context_version: int | None = None
    policy_version: int | None = None


class SubagentId(Params):
    subagent_run_id: StrictStr
    wait_seconds: float = Field(default=0, ge=0, le=30)
    after_seq: StrictInt = Field(default=0, ge=0)


class PermissionParams(Params):
    workspace: StrictStr
    action: Literal["list", "revoke", "clear"] = "list"
    session_id: str = ""
    rule_id: str | None = None


class SubagentList(Params):
    parent_run_id: StrictStr


class Event(Contract):
    type: StrictStr
    event_id: str | None = None
    run_id: str | None = None
    context_version: int = 0
    stream_id: str | None = None
    tool_call_id: str | None = None


class StreamEvent(Event):
    type: Literal["llm_stream_start", "llm_token", "llm_stream_end"]
    text: str = ""
    status: Literal["finished", "failed", "interrupted"] | None = None
    generation_attempt: int = 0
    finish_reason: str | None = None


class ModelRequestEvent(Event):
    type: Literal["llm_request"]
    node: str = ""
    role: str = ""
    generation_attempt: int = 0
    message_count: int = Field(ge=0)
    input_estimate: int = Field(ge=0)
    output_budget: int = Field(gt=0)


class ModelResponseEvent(Event):
    type: Literal["llm_response"]
    node: str = ""
    role: str = ""
    generation_attempt: int = 0
    status: Literal["finished", "failed", "interrupted"]
    finish_reason: str | None = None
    raw_finish_reason: str | None = None


class ModelUsageEvent(Event):
    type: Literal["model_usage"]
    agent: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_read_tokens: int | None = Field(default=None, ge=0)
    cache_creation_tokens: int | None = Field(default=None, ge=0)
    estimated: bool = False
    reported: bool = False
    duration_ms: float = Field(ge=0)
    usage_source: str = "unknown"


class MCPDiagnosticEvent(Event):
    type: Literal["mcp_diagnostic"]
    server: str
    phase: str
    status: str
    duration_ms: float = Field(ge=0)
    stderr: str = ""
    output_truncated: bool = False


class ApprovalData(Contract):
    id: str
    command: str
    risk_reason: str
    tool_name: str = "BashTool"
    tool_call_id: str = ""
    run_id: str = ""
    args_digest: str = ""
    context_version: int = 0
    policy_version: int = 0
    rule_preview: str = ""


class ApprovalEvent(Event):
    type: Literal["daemon_approval_request"]
    request: ApprovalData


class RunEvent(Event):
    type: Literal["run_started", "run_finished"]
    status: Literal["finished", "failed", "interrupted"] | None = None
    stop_reason: str | None = None


class ToolEvent(Event):
    type: Literal["tool_call", "tool_result"]
    name: str
    args: dict[str, Any] = Field(default_factory=dict)
    result: Any = None


class EventEnvelope(Contract):
    run_id: str
    seq: int = Field(ge=1)
    event: dict[str, Any]


PARAMS: dict[str, type[Params]] = {
    "permissions": PermissionParams,
    "start_run": StartRun,
    "subscribe": Subscribe,
    "submit_run_message": SubmitMessage,
    "cancel_run": RunId,
    "compact_run": RunId,
    "compact_status": CompactId,
    "compact_cancel": CompactId,
    "resolve_approval": ApprovalResponse,
    "subagent_get": SubagentId,
    "subagent_cancel": SubagentId,
    "subagent_events": SubagentId,
    "subagent_list": SubagentList,
    "status": Params,
    "shutdown": Params,
}
EVENTS: dict[str, type[Event]] = {
    name: StreamEvent for name in ["llm_stream_start", "llm_token", "llm_stream_end"]
}
EVENTS.update(
    {
        "llm_request": ModelRequestEvent,
        "llm_response": ModelResponseEvent,
        "model_usage": ModelUsageEvent,
        "mcp_diagnostic": MCPDiagnosticEvent,
        "daemon_approval_request": ApprovalEvent,
    }
)
EVENTS.update({name: RunEvent for name in ["run_started", "run_finished"]})
EVENTS.update({name: ToolEvent for name in ["tool_call", "tool_result"]})


def validate_event(event: dict[str, Any]) -> dict[str, Any]:
    model = EVENTS.get(str(event.get("type")), Event)
    return model.model_validate(event).model_dump(exclude_unset=True)
