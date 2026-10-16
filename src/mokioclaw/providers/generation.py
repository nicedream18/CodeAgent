"""Provider-independent completion checks. Parsed partial JSON is never executable."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol

FinishReason = Literal["completed", "tool_calls", "output_limit", "blocked", "unknown"]


class ModelAdapter(Protocol):
    def invoke(self, messages: list[Any]) -> Any: ...


class ModelGenerationStopped(RuntimeError):
    def __init__(self, reason: str, message: Any = None):
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.stream_id = ""
        self.state: dict[str, Any] = {}


@dataclass(frozen=True)
class GenerationOutcome:
    reason: FinishReason
    raw_reason: str | None


def outcome(message: Any) -> GenerationOutcome:
    metadata = getattr(message, "response_metadata", {}) or {}
    raw = metadata.get("finish_reason") or metadata.get("stop_reason")
    if raw is None:
        return GenerationOutcome("completed", None)  # legacy/injected models
    if raw in {"stop", "end_turn", "stop_sequence"}:
        return GenerationOutcome("completed", str(raw))
    if raw in {"tool_calls", "tool_use", "function_call"}:
        return GenerationOutcome("tool_calls", str(raw))
    if raw in {"length", "max_tokens"}:
        return GenerationOutcome("output_limit", str(raw))
    if raw in {"content_filter", "refusal", "blocked"}:
        return GenerationOutcome("blocked", str(raw))
    return GenerationOutcome("unknown", str(raw))


def declared_calls(message: Any) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for item in [*getattr(message, "tool_calls", []), *getattr(message, "invalid_tool_calls", [])]:
        key = str(item.get("id") or len(calls))
        calls[key] = {
            "id": key,
            "name": item.get("name") or "unknown_tool",
            "args": item.get("args", {}),
        }
    for item in getattr(message, "tool_call_chunks", []):
        key = str(item.get("id") or item.get("index", len(calls)))
        calls[key] = {
            "id": key,
            "name": item.get("name") or "unknown_tool",
            "args": item.get("args", ""),
        }
    return list(calls.values())


def validate_generation(message: Any) -> GenerationOutcome:
    result = outcome(message)
    if result.reason == "output_limit":
        raise ModelGenerationStopped("model_output_truncated", message)
    if result.reason in {"blocked", "unknown"}:
        raise ModelGenerationStopped(
            "model_content_blocked"
            if result.reason == "blocked"
            else "model_finish_reason_unknown",
            message,
        )
    try:
        for call in getattr(message, "tool_call_chunks", []):
            if not isinstance(
                json.loads(call.get("args", ""), parse_constant=_invalid_constant), dict
            ):
                raise ValueError("tool arguments must be an object")
        for call in getattr(message, "additional_kwargs", {}).get("tool_calls", []):
            if not isinstance(
                json.loads(call["function"]["arguments"], parse_constant=_invalid_constant), dict
            ):
                raise ValueError("tool arguments must be an object")
        if getattr(message, "invalid_tool_calls", []):
            raise ValueError("invalid tool calls")
    except (ValueError, TypeError, KeyError) as exc:
        raise ModelGenerationStopped("model_tool_arguments_invalid", message) from exc
    return result


def _invalid_constant(value: str) -> Any:
    raise ValueError(f"Non-JSON constant: {value}")
