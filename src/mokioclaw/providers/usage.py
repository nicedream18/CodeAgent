"""Normalize compatible provider usage without counting cached input twice."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    estimated: bool = False
    source: str


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def normalize_usage(response: Any, input_estimate: int) -> ModelUsage:
    usage = getattr(response, "usage_metadata", None) or {}
    metadata = getattr(response, "response_metadata", None) or {}
    raw = metadata.get("token_usage") or metadata.get("usage") or {}
    incoming = _count(usage.get("input_tokens", raw.get("prompt_tokens")))
    outgoing = _count(usage.get("output_tokens", raw.get("completion_tokens")))
    reported = incoming is not None and outgoing is not None
    details = usage.get("input_token_details") or {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    cache_read = _count(
        details.get(
            "cache_read", prompt_details.get("cached_tokens", raw.get("prompt_cache_hit_tokens"))
        )
    )
    cache_creation = _count(details.get("cache_creation", raw.get("cache_creation_input_tokens")))
    if incoming is None:
        incoming = input_estimate
    if outgoing is None:
        outgoing = (
            (
                len(str(getattr(response, "content", "")).encode())
                + len(str(getattr(response, "tool_calls", "")).encode())
            )
            if response is not None
            else 0
        )
    return ModelUsage(
        input_tokens=incoming,
        output_tokens=outgoing,
        total_tokens=incoming + outgoing,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        estimated=not reported,
        source="usage_metadata"
        if usage
        else "response_metadata.token_usage"
        if raw
        else "byte_estimate",
    )
