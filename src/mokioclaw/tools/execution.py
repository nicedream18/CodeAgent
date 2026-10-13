from __future__ import annotations

import time
from typing import Any, Callable

from langchain_core.tools import BaseTool
from pydantic import ValidationError

from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_context import ToolCallContext, reset_tool_call_context, set_tool_call_context
from mokioclaw.core.tool_policy import authorize_tool
from mokioclaw.tools.registry import tool_spec_for


class ToolExecutionError(Exception):
    def __init__(self, error_type: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.retryable = retryable


def classify_tool_failure(error: Any) -> tuple[str, bool]:
    if isinstance(error, ToolExecutionError):
        return error.error_type, error.retryable
    if isinstance(error, ValidationError):
        return "invalid_input", False
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "transient", True
    text = str(error).lower()
    if text in {"transient", "timeout", "rate_limited"}:
        return "transient", True
    if text in {"permission_denied", "invalid_input", "schema_error"}:
        return ("permission_denied", False) if text == "permission_denied" else ("invalid_input", False)
    if any(marker in text for marker in ("timed out", "timeout", "temporarily unavailable", "connection reset", "connection refused", "429", "rate limit")):
        return "transient", True
    if any(marker in text for marker in ("permission", "denied", "approval")):
        return "permission_denied", False
    if any(marker in text for marker in ("validation", "invalid", "missing required", "unknown tool")):
        return "invalid_input", False
    return "tool_error", False


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
        return {"ok": False, "error": str(exc), "error_type": "invalid_input", "retryable": False, "retries": 0}
    return None


def execute_tool(
    runtime: RuntimeState,
    tool: BaseTool,
    args: dict[str, Any],
    *,
    on_retry: Callable[[dict[str, Any]], None] | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
    tool_call_id: str | None = None,
) -> Any:
    call_id = tool_call_id or f"{tool.name}-call"

    # Reject malformed arguments before opening an approval prompt.
    invalid = _validate_tool_args(tool, args)
    if invalid is not None:
        if on_event:
            on_event({"type": "tool_validation_failed", "tool_call_id": call_id, "tool_name": tool.name, "error_type": "invalid_input"})
        return invalid

    denied = authorize_tool(runtime, tool.name, args, tool_call_id=call_id, event_writer=on_event)
    if denied is not None:
        return denied

    spec = tool_spec_for(tool.name)
    is_idempotent = bool(spec and spec.idempotent)
    max_retries = max(0, int(getattr(runtime, "tool_retry_limit", 2))) if is_idempotent else 0
    context_token = set_tool_call_context(ToolCallContext(call_id, tool.name, permission_checked=True, event_writer=on_event))
    try:
        for attempt in range(max_retries + 1):
            try:
                result = tool.invoke(args)
                if isinstance(result, dict) and result.get("ok") is False:
                    explicit_type = result.get("error_type")
                    error_type, classified_retryable = classify_tool_failure(explicit_type or result.get("error", ""))
                    retryable = bool(result.get("retryable", classified_retryable))
                    if retryable and attempt < max_retries:
                        delay = min(0.25 * (2**attempt), 2.0)
                        if on_retry:
                            on_retry({"type": "tool_retry", "tool_call_id": call_id, "name": tool.name, "attempt": attempt + 1, "delay_seconds": delay, "error_type": error_type})
                        time.sleep(delay)
                        continue
                    return _truncate_result({**result, "error_type": explicit_type or error_type, "retryable": retryable, "retries": attempt}, runtime.context_output_limit_chars)
                return _truncate_result(result, runtime.context_output_limit_chars)
            except Exception as exc:
                error_type, retryable = classify_tool_failure(exc)
                if retryable and is_idempotent and attempt < max_retries:
                    delay = min(0.25 * (2**attempt), 2.0)
                    if on_retry:
                        on_retry({"type": "tool_retry", "tool_call_id": call_id, "name": tool.name, "attempt": attempt + 1, "delay_seconds": delay, "error_type": error_type})
                    time.sleep(delay)
                    continue
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "error_type": error_type, "retryable": retryable, "retries": attempt}
        return {"ok": False, "error": "tool retry loop exhausted", "error_type": "tool_error", "retryable": False}
    finally:
        reset_tool_call_context(context_token)


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
