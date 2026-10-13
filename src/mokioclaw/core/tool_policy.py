from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Callable

from mokioclaw.core.approval import (
    ApprovalDecision,
    ApprovalRequest,
    classify_command_risk,
    classify_dangerous_command,
    make_approval_request,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_context import current_tool_call_context
from mokioclaw.tools.registry import tool_spec_for


VALID_POLICIES = {"allow", "ask", "deny"}


def parse_tool_policies(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MOKIO_TOOL_POLICIES must be a JSON object") from exc
    if not isinstance(payload, dict):
        raise ValueError("MOKIO_TOOL_POLICIES must be a JSON object")
    policies: dict[str, str] = {}
    for name, policy in payload.items():
        normalized = str(policy).strip().lower()
        if normalized not in VALID_POLICIES:
            raise ValueError(f"invalid policy for {name}: expected allow, ask, or deny")
        policies[str(name)] = normalized
    return policies


def parse_bash_patterns(raw: str | None, *, variable: str) -> list[str]:
    """Parse a JSON list of regex patterns and reject invalid expressions early."""
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{variable} must be a JSON list of regular expressions") from exc
    if not isinstance(payload, list) or any(not isinstance(item, str) or not item for item in payload):
        raise ValueError(f"{variable} must be a JSON list of non-empty strings")
    for pattern in payload:
        try:
            re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"invalid regular expression in {variable}: {pattern}") from exc
    return list(payload)


def _emit(writer: Callable[[dict[str, Any]], None] | None, event_type: str, **payload: Any) -> None:
    if writer is not None:
        writer({"type": event_type, **payload})


def _deny(
    name: str,
    message: str,
    *,
    tool_call_id: str,
    event_writer: Callable[[dict[str, Any]], None] | None,
    reason: str = "policy_denied",
) -> dict[str, Any]:
    _emit(event_writer, "permission_denied", tool_call_id=tool_call_id, tool_name=name, decision=reason, reason=message)
    return {"ok": False, "error": message, "error_type": "permission_denied", "retryable": False}


def _outside_workspace_command(command: str, workspace: Path) -> bool:
    """Conservative shell heuristic for explicit paths and directory changes."""
    if re.search(r"(^|\s)~(?:/|\s|$)|\$\{?(?:HOME|PWD)\b", command):
        return True
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        # Unparseable quoting is ambiguous; require approval.
        return True

    workspace_root = workspace.resolve()
    for index, token in enumerate(tokens):
        candidates: list[str] = []
        if token in {"cd", "pushd"} and index + 1 < len(tokens):
            candidates.append(tokens[index + 1])
        elif token.startswith("/") and not token.startswith("//"):
            # Ignore URL-like strings and harmless shell descriptor syntax.
            if "://" not in token and token not in {"/dev/null"}:
                candidates.append(token)
        elif re.match(r"^[A-Za-z]:[\\/]", token):
            candidates.append(token)
        elif token.startswith("../") or token == ".." or "/../" in token:
            candidates.append(token)

        for candidate in candidates:
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                path = workspace_root / path
            try:
                resolved = path.resolve()
                if resolved != workspace_root and workspace_root not in resolved.parents:
                    return True
            except (OSError, RuntimeError):
                return True
    return False


def _approval_preview(name: str, args: dict[str, Any]) -> str:
    if name == "BashTool":
        value = str(args.get("command", ""))
        value = re.sub(
            r"(?i)\b([A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET|API_KEY|ACCESS_KEY))=([^\s;&|]+)",
            r"\1=[redacted]",
            value,
        )
        value = re.sub(r"(?i)(--(?:token|password|secret|api-key)\s+)([^\s]+)", r"\1[redacted]", value)
        label = "command"
    elif name in {"FileReadTool", "FileWriteTool", "FileEditTool"}:
        value = str(args.get("file_path", ""))
        label = "file_path"
        if name == "FileWriteTool":
            value += f" (content: {len(str(args.get('content', '')))} characters)"
        elif name == "FileEditTool":
            value += f" (replacement: {len(str(args.get('new_text', '')))} characters)"
    else:
        safe: dict[str, Any] = {}
        for key, value in args.items():
            if any(secret in str(key).lower() for secret in ("password", "secret", "token", "api_key", "credential")):
                safe[key] = "[redacted]"
            elif isinstance(value, str):
                safe[key] = value if len(value) <= 100 else value[:100] + "…"
            else:
                safe[key] = value
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)[:500]
    return f"{label}={value[:400]}" if len(value) > 400 else f"{label}={value}"


def _request_approval(
    runtime: RuntimeState,
    name: str,
    args: dict[str, Any],
    *,
    reason: str,
    tool_call_id: str,
    event_writer: Callable[[dict[str, Any]], None] | None,
) -> dict[str, Any] | None:
    preview = _approval_preview(name, args)
    base_request = make_approval_request(preview, reason)
    request = ApprovalRequest(
        id=base_request.id,
        command=preview,
        risk_reason=reason,
        tool_name=name,
        tool_call_id=tool_call_id,
    )
    if runtime.approval_mode == "auto":
        _emit(event_writer, "permission_granted", tool_call_id=tool_call_id, approval_id=request.id, tool_name=name, decision="auto_approved")
        return None
    if runtime.approval_mode == "deny" or runtime.approval_handler is None:
        message = f"human approval required for tool: {name}"
        _emit(event_writer, "permission_denied", tool_call_id=tool_call_id, approval_id=request.id, tool_name=name, decision="approval_unavailable", reason=message)
        return {"ok": False, "requires_approval": True, "approval_id": request.id, "approved": False, "error": message, "error_type": "permission_denied", "retryable": False}

    _emit(
        event_writer,
        "permission_requested",
        tool_call_id=tool_call_id,
        approval_id=request.id,
        tool_name=name,
        params_preview=preview,
        risk_reason=reason,
    )
    decision = runtime.approval_handler(request)
    approved = decision.approved if isinstance(decision, ApprovalDecision) else bool(decision)
    if approved:
        _emit(event_writer, "permission_granted", tool_call_id=tool_call_id, approval_id=request.id, tool_name=name, decision="approved_once")
        return None
    rejection = decision.reason if isinstance(decision, ApprovalDecision) else ""
    message = rejection or f"human rejected tool: {name}"
    _emit(event_writer, "permission_denied", tool_call_id=tool_call_id, approval_id=request.id, tool_name=name, decision="rejected_once", reason=message)
    return {"ok": False, "requires_approval": True, "approval_id": request.id, "approved": False, "error": message, "error_type": "permission_denied", "retryable": False}


def authorize_tool(
    runtime: RuntimeState,
    name: str,
    args: dict[str, Any],
    *,
    tool_call_id: str = "",
    event_writer: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Enforce nested agent/skill scopes and the configured per-call policy."""
    call_id = tool_call_id or (current_tool_call_context().tool_call_id if current_tool_call_context() else "")
    spec = tool_spec_for(name)
    if spec is None:
        return _deny(name, f"unknown tool: {name}", tool_call_id=call_id, event_writer=event_writer, reason="unknown_tool")

    if runtime.active_skill_tools is not None and name not in runtime.active_skill_tools and name not in {"LoadSkillTool", "TodoUpdateTool"}:
        return _deny(name, f"tool is outside the active Skill scope: {name}", tool_call_id=call_id, event_writer=event_writer, reason="skill_scope")
    agent_allowed = runtime.active_agent_tools is None or name in runtime.active_agent_tools or (name.startswith("MCP_") and "MCP_*" in runtime.active_agent_tools)
    if not agent_allowed:
        return _deny(name, f"tool is outside the active Subagent scope: {name}", tool_call_id=call_id, event_writer=event_writer, reason="subagent_scope")

    capabilities = spec.capabilities
    if runtime.active_skill_permissions is not None and capabilities and not capabilities.issubset(runtime.active_skill_permissions):
        missing = ", ".join(sorted(capabilities - runtime.active_skill_permissions))
        return _deny(name, f"permission is outside the active Skill scope: {missing}", tool_call_id=call_id, event_writer=event_writer, reason="skill_permission")

    policy = runtime.tool_policies.get(name, spec.default_policy)
    command = str(args.get("command", "")) if name == "BashTool" else ""

    # 1. Dangerous Bash commands are hard-denied before prompting.
    if command:
        dangerous = classify_dangerous_command(command)
        if dangerous:
            return _deny(name, f"blocked potentially dangerous command pattern: {dangerous}", tool_call_id=call_id, event_writer=event_writer, reason="dangerous_command")

    # 2. Explicit per-command deny rules always win.
    if command:
        for pattern in runtime.bash_deny_patterns:
            if re.search(pattern, command, flags=re.IGNORECASE):
                return _deny(name, f"command denied by Bash policy pattern: {pattern}", tool_call_id=call_id, event_writer=event_writer, reason="bash_deny_pattern")

    # 3. Operations that may leave the workspace always require a human decision.
    if policy == "deny":
        return _deny(name, f"tool denied by policy: {name}", tool_call_id=call_id, event_writer=event_writer, reason="tool_policy_deny")
    if command and _outside_workspace_command(command, runtime.workspace):
        denied = _request_approval(runtime, name, args, reason="Command may access paths outside the workspace", tool_call_id=call_id, event_writer=event_writer)
        if denied is not None:
            return denied
        return None
    if policy == "ask" and name != "BashTool":
        denied = _request_approval(runtime, name, args, reason=f"Tool policy requires approval for {name}", tool_call_id=call_id, event_writer=event_writer)
        if denied is not None:
            return denied
        return None
    if command:
        # Explicit allow patterns are opt-in exceptions to the legacy
        # high-risk command classifier. They cannot bypass forced outside-
        # workspace approval or explicit deny rules above.
        if any(re.search(pattern, command, flags=re.IGNORECASE) for pattern in runtime.bash_allow_patterns):
            _emit(event_writer, "permission_granted", tool_call_id=call_id, tool_name=name, decision="bash_allow_pattern")
            return None
        risk_reason = classify_command_risk(command)
        if risk_reason:
            denied = _request_approval(runtime, name, args, reason=risk_reason, tool_call_id=call_id, event_writer=event_writer)
            if denied is not None:
                return denied
            return None
    _emit(event_writer, "permission_granted", tool_call_id=call_id, tool_name=name, decision="policy_allow" if policy == "allow" else "default_allow")
    return None
