from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_context import (
    ToolCallContext,
    reset_tool_call_context,
    set_tool_call_context,
)
from mokioclaw.core.tool_policy import authorize_tool
from mokioclaw.tools.registry import tool_spec_for

ToolErrorType = Literal[
    "invalid_input",
    "permission_denied",
    "transient",
    "outcome_unknown",
    "tool_error",
]
_TOOL_ERROR_TYPES = frozenset(
    {"invalid_input", "permission_denied", "transient", "outcome_unknown", "tool_error"}
)
_RETRYABLE_ERROR_TYPES = frozenset({"transient"})


class ToolExecutionError(Exception):
    """A tool failure with an explicit, stable category.

    ``retryable`` is a tool's recovery hint. The execution policy still decides
    whether another attempt is safe based on idempotency and the retry budget.
    """

    def __init__(
        self, error_type: ToolErrorType | str, message: str, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.error_type = (
            error_type if error_type in _TOOL_ERROR_TYPES else "tool_error"
        )
        self.retryable = bool(retryable and self.error_type in _RETRYABLE_ERROR_TYPES)


def classify_tool_failure(error: Any) -> tuple[str, bool]:
    if isinstance(error, ToolExecutionError):
        return error.error_type, error.retryable
    if isinstance(error, ValidationError):
        return "invalid_input", False
    if isinstance(error, dict):
        explicit_type = error.get("error_type")
        if explicit_type is not None:
            normalized = _explicit_error_type(explicit_type) or "tool_error"
            return normalized, normalized in _RETRYABLE_ERROR_TYPES
        if error.get("timed_out") is True:
            return "transient", True
        error = error.get("error", "")
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "transient", True
    text = str(error).lower()
    if text in {"transient", "timeout", "rate_limited"}:
        return "transient", True
    if text in {"permission_denied", "invalid_input", "schema_error"}:
        return (
            ("permission_denied", False)
            if text == "permission_denied"
            else ("invalid_input", False)
        )
    if any(
        marker in text
        for marker in (
            "timed out",
            "timeout",
            "temporarily unavailable",
            "connection reset",
            "connection refused",
            "429",
            "rate limit",
        )
    ):
        return "transient", True
    if any(marker in text for marker in ("permission", "denied", "approval")):
        return "permission_denied", False
    if any(
        marker in text
        for marker in ("validation", "invalid", "missing required", "unknown tool")
    ):
        return "invalid_input", False
    return "tool_error", False


def tool_failure_result(
    error: Any,
    *,
    tool_name: str = "",
    retry_stop_reason: str | None = None,
) -> dict[str, Any]:
    """Normalize an error raised outside the tool invocation loop."""
    error_type, recoverable = classify_tool_failure(error)
    message = str(error)
    result: dict[str, Any] = {
        "ok": False,
        "error": message,
        "error_type": error_type,
        "retryable": False,
        "retries": 0,
        "retry_stop_reason": retry_stop_reason
        or ("not_recoverable" if not recoverable else "not_retried"),
    }
    if tool_name:
        result["tool_name"] = tool_name
    return result


def _explicit_error_type(value: Any) -> str | None:
    if isinstance(value, str) and value in _TOOL_ERROR_TYPES:
        return value
    return None


def _failure_result(
    *,
    error: Any,
    error_type: str,
    tool_name: str,
    attempt: int,
    is_idempotent: bool,
    max_retries: int,
    tool_retry_hint: bool,
) -> dict[str, Any]:
    recoverable = error_type in _RETRYABLE_ERROR_TYPES and tool_retry_hint
    if not recoverable:
        stop_reason = "not_recoverable"
    elif not is_idempotent:
        stop_reason = "non_idempotent_side_effect"
    elif attempt >= max_retries:
        stop_reason = "retry_limit_reached"
    else:
        stop_reason = "retry_budget_available"
    return {
        "ok": False,
        "error": str(error),
        "error_type": error_type,
        "retryable": stop_reason == "retry_budget_available",
        "retries": attempt,
        "retry_stop_reason": stop_reason,
        "tool_name": tool_name,
    }


def _validate_tool_args(tool: BaseTool, args: dict[str, Any]) -> dict[str, Any] | None:
    schema = getattr(tool, "args_schema", None)
    if schema is None:
        return None
    try:
        if hasattr(schema, "model_validate"):
            schema.model_validate(args)
        elif hasattr(schema, "parse_obj"):
            schema.parse_obj(args)
        else:
            return None
    except ValidationError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_type": "invalid_input",
            "retryable": False,
            "retries": 0,
            "retry_stop_reason": "not_recoverable",
        }
    return None


def execute_tool(
    runtime: RuntimeState,
    tool: BaseTool,
    args: dict[str, Any],
    *,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    tool_call_id: str | None = None,
    budget_reserved: bool = False,
) -> Any:
    call_id = tool_call_id or f"{tool.name}-call"

    if not budget_reserved:
        budget_failure = reserve_subagent_tool_call(
            runtime, tool.name, call_id, on_event=on_event
        )
        if budget_failure is not None:
            return budget_failure

    def skill_event(result: Any = None, error: Exception | None = None) -> None:
        if tool.name != "LoadSkillTool" or on_event is None:
            return
        if isinstance(result, dict) and result.get("ok") is True:
            on_event(
                {
                    "type": "skill_loaded",
                    "tool_call_id": call_id,
                    "skill_name": result.get("name", ""),
                    "source": result.get("source", ""),
                    "path": result.get("path", ""),
                }
            )
            return
        diagnostic = result.get("diagnostic", {}) if isinstance(result, dict) else {}
        requested_name = str(args.get("name", ""))
        if error is not None and not diagnostic:
            diagnostic = {
                "code": "load_error",
                "message": f"{type(error).__name__}: {error}",
                "skill_name": requested_name,
                "path": "",
            }
        path = Path(str(diagnostic.get("path", ""))) if diagnostic.get("path") else None
        source = (
            "workspace"
            if path is not None and path.is_relative_to(runtime.workspace.resolve())
            else "user"
            if path is not None
            else ""
        )
        on_event(
            {
                "type": "skill_load_failed",
                "tool_call_id": call_id,
                "skill_name": diagnostic.get("skill_name", requested_name),
                "source": source,
                "diagnostic": diagnostic,
            }
        )

    # Reject malformed arguments before opening an approval prompt.
    invalid = _validate_tool_args(tool, args)
    if invalid is not None:
        skill_event(invalid)
        if on_event:
            on_event(
                {
                    "type": "tool_validation_failed",
                    "tool_call_id": call_id,
                    "tool_name": tool.name,
                    "error_type": "invalid_input",
                }
            )
        return {**invalid, "tool_name": tool.name}

    denied = authorize_tool(
        runtime, tool.name, args, tool_call_id=call_id, event_writer=on_event
    )
    if denied is not None:
        return {
            **denied,
            "error_type": _explicit_error_type(denied.get("error_type"))
            or "permission_denied",
            "retryable": False,
            "retries": 0,
            "retry_stop_reason": "permission_denied",
            "tool_name": tool.name,
        }

    spec = tool_spec_for(tool.name)
    is_idempotent = bool(spec and spec.idempotent)
    max_retries = (
        max(0, int(getattr(runtime, "tool_retry_limit", 2))) if is_idempotent else 0
    )
    context_token = set_tool_call_context(
        ToolCallContext(
            call_id, tool.name, permission_checked=True, event_writer=on_event
        )
    )
    try:
        for attempt in range(max_retries + 1):
            try:
                result = tool.invoke(args)
                skill_event(result)
                if isinstance(result, dict) and result.get("ok") is False:
                    error_type, classified_retryable = classify_tool_failure(result)
                    tool_retry_hint = bool(
                        result.get("retryable", classified_retryable)
                    )
                    can_retry = (
                        error_type in _RETRYABLE_ERROR_TYPES
                        and tool_retry_hint
                        and is_idempotent
                        and attempt < max_retries
                    )
                    if can_retry:
                        delay = min(0.25 * (2**attempt), 2.0)
                        if on_retry:
                            retry_reason = (
                                result.get("retry_reason")
                                or result.get("error")
                                or "tool reported a transient failure"
                            )
                            on_retry(
                                {
                                    "type": "tool_retry",
                                    "tool_call_id": call_id,
                                    "name": tool.name,
                                    "attempt": attempt + 1,
                                    "delay_seconds": delay,
                                    "error_type": error_type,
                                    "retry_reason": str(retry_reason),
                                }
                            )
                        time.sleep(delay)
                        continue
                    error_message = result.get("error") or (
                        "tool execution timed out"
                        if result.get("timed_out")
                        else error_type
                    )
                    failure = _failure_result(
                        error=error_message,
                        error_type=error_type,
                        tool_name=tool.name,
                        attempt=attempt,
                        is_idempotent=is_idempotent,
                        max_retries=max_retries,
                        tool_retry_hint=tool_retry_hint,
                    )
                    return _truncate_result(
                        {**result, **failure}, runtime.context_output_limit_chars
                    )
                return _truncate_result(result, runtime.context_output_limit_chars)
            except Exception as exc:
                skill_event(error=exc)
                error_type, retryable = classify_tool_failure(exc)
                can_retry = (
                    error_type in _RETRYABLE_ERROR_TYPES
                    and retryable
                    and is_idempotent
                    and attempt < max_retries
                )
                if can_retry:
                    delay = min(0.25 * (2**attempt), 2.0)
                    if on_retry:
                        on_retry(
                            {
                                "type": "tool_retry",
                                "tool_call_id": call_id,
                                "name": tool.name,
                                "attempt": attempt + 1,
                                "delay_seconds": delay,
                                "error_type": error_type,
                                "retry_reason": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    time.sleep(delay)
                    continue
                stop_reason = (
                    "retry_limit_reached"
                    if retryable and is_idempotent and attempt >= max_retries
                    else None
                )
                if retryable and not is_idempotent:
                    stop_reason = "non_idempotent_side_effect"
                failure = _failure_result(
                    error=f"{type(exc).__name__}: {exc}",
                    error_type=error_type,
                    tool_name=tool.name,
                    attempt=attempt,
                    is_idempotent=is_idempotent,
                    max_retries=max_retries,
                    tool_retry_hint=retryable,
                )
                if stop_reason:
                    failure["retry_stop_reason"] = stop_reason
                return failure
        return {
            "ok": False,
            "error": "tool retry loop exhausted",
            "error_type": "tool_error",
            "retryable": False,
            "retries": max_retries,
            "retry_stop_reason": "retry_limit_reached",
            "tool_name": tool.name,
        }
    finally:
        reset_tool_call_context(context_token)


def reserve_subagent_tool_call(
    runtime: RuntimeState,
    tool_name: str,
    tool_call_id: str,
    *,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Reserve one model-requested tool call, including invalid or denied calls."""
    budget = runtime.active_subagent_budget
    if budget is None:
        return None
    cancel_event = runtime.subagent_cancel_event
    if cancel_event is not None and cancel_event.is_set():
        budget.cancelled = True
        budget.exhausted = True
        writer = on_event or budget.writer
        if writer is not None:
            writer(
                {
                    "type": "subagent_cancel_observed",
                    "handoff_id": budget.handoff_id,
                    "subagent": budget.subagent_name,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                }
            )
        return {
            "ok": False,
            "error": "Subagent cancellation requested",
            "error_type": "cancelled",
            "retryable": False,
            "retries": 0,
            "retry_stop_reason": "cancelled",
            "tool_name": tool_name,
        }
    if budget.reserve():
        return None

    if not budget.exhaustion_event_emitted:
        budget.exhaustion_event_emitted = True
        writer = on_event or budget.writer
        if writer is not None:
            writer(
                {
                    "type": "subagent_budget_exhausted",
                    "handoff_id": budget.handoff_id,
                    "subagent": budget.subagent_name,
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "limit": budget.limit,
                    "used": budget.used,
                }
            )
    return {
        "ok": False,
        "error": f"Subagent tool-call budget exhausted ({budget.limit})",
        "error_type": "budget_exhausted",
        "retryable": False,
        "retries": 0,
        "retry_stop_reason": "tool_budget_exhausted",
        "tool_name": tool_name,
        "handoff_id": budget.handoff_id,
        "budget_limit": budget.limit,
        "budget_used": budget.used,
    }


def _truncate_result(value: Any, limit: int) -> Any:
    truncated = False
    omitted = 0

    def visit(item: Any) -> Any:
        nonlocal truncated, omitted
        if isinstance(item, str) and len(item) > limit:
            truncated = True
            omitted += len(item) - limit
            return item[:limit] + f"\n… [{len(item) - limit} characters omitted]"
        if isinstance(item, dict):
            return {key: visit(child) for key, child in item.items()}
        if isinstance(item, list):
            return [visit(child) for child in item]
        return item

    result = visit(value)
    if truncated and isinstance(result, dict):
        result["output_truncated"] = True
        result["omitted_characters"] = omitted
    return result
