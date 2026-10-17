"""Request budgets, independent of graph scheduling and model transport."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any


class ContextBudgetError(ValueError):
    """A request cannot fit without discarding required instructions."""


@dataclass(frozen=True)
class ContextBudget:
    window: int = 400000
    output: int = 8192
    safety: int = 1024
    high_ratio: float = 0.85
    target_ratio: float = 0.60

    def __post_init__(self) -> None:
        if self.window <= 0 or self.output <= 0 or self.safety < 0:
            raise ContextBudgetError(
                "context window/output must be positive; safety must be nonnegative"
            )
        if self.input_limit <= 0:
            raise ContextBudgetError("context window must exceed output reserve plus safety margin")
        if not 0 < self.target_ratio < self.high_ratio <= 1:
            raise ContextBudgetError("require 0 < context target ratio < high ratio <= 1")

    @property
    def input_limit(self) -> int:
        return self.window - self.output - self.safety

    @property
    def high(self) -> int:
        return max(1, int(self.input_limit * self.high_ratio))

    @property
    def target(self) -> int:
        return max(1, int(self.input_limit * self.target_ratio))

    @classmethod
    def from_runtime(cls, runtime: Any = None) -> ContextBudget:
        from mokioclaw.providers.model_policy import REQUEST_BUDGET

        selected = REQUEST_BUDGET.get()
        if selected is not None:
            return selected

        def setting(attr: str, env: str, default: Any, cast=int):
            value = getattr(runtime, attr, None)
            return cast(value if value is not None else os.getenv(env, default))

        return cls(
            window=setting("context_token_limit", "MOKIO_CONTEXT_TOKEN_LIMIT", 400000),
            output=setting(
                "model_context_reserve_tokens",
                "MOKIO_MODEL_CONTEXT_RESERVE_TOKENS",
                8192,
            ),
            safety=setting("context_safety_tokens", "MOKIO_CONTEXT_SAFETY_TOKENS", 1024),
            high_ratio=setting("context_high_ratio", "MOKIO_CONTEXT_HIGH_RATIO", 0.85, float),
            target_ratio=setting("context_target_ratio", "MOKIO_CONTEXT_TARGET_RATIO", 0.60, float),
        )


@dataclass(frozen=True)
class BudgetSnapshot:
    input_tokens: int
    window: int
    reserved_tokens: int
    safety_tokens: int
    token_limit: int
    high_watermark: int
    target_tokens: int
    estimation_method: str
    estimated: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def estimate_messages(
    model: Any, messages: list[Any], tools: list[Any] | None = None
) -> tuple[int, str]:
    """Count messages and schemas using an adapter or a conservative fallback.

    Counting is always labelled an estimate: providers add transport-specific
    wrappers. Compatible adapters use a bundled, labelled tokenizer estimate;
    unsupported custom models retain the conservative UTF-8 byte fallback.
    """
    schemas = tools if tools is not None else getattr(model, "kwargs", {}).get("tools", [])
    adapter = getattr(model, "estimate_context_tokens", None)
    if not callable(adapter):
        adapter = getattr(getattr(model, "bound", None), "estimate_context_tokens", None)
    if callable(adapter):
        try:
            count, method = adapter(messages, schemas)
            if int(count) >= 0:
                return int(count), str(method)
        except (ValueError, OSError, TypeError):
            pass  # Retain the conservative fallback if the local adapter fails.
    schema_text = json.dumps(schemas, ensure_ascii=False, default=str)
    schema_count = len(schema_text.encode("utf-8")) if schemas else 0
    counter = None if callable(adapter) else getattr(model, "get_num_tokens_from_messages", None)
    if not callable(counter) and not callable(adapter):
        counter = getattr(getattr(model, "bound", None), "get_num_tokens_from_messages", None)
    if callable(counter):
        try:
            # Do not pass tools to counters which silently ignore their schema.
            count = int(counter(messages))
            if count >= 0:
                return count + schema_count, "model_counter+schema_bytes"
        except Exception:  # noqa: BLE001 - arbitrary provider counters may fail
            counter = None  # The labelled conservative fallback below is intentional.
    payload = [
        message.model_dump(exclude_none=True) if hasattr(message, "model_dump") else str(message)
        for message in messages
    ]
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return max(1, len(text.encode("utf-8")) + schema_count + 16 * len(messages)), "utf8_bytes"


def snapshot(model: Any, messages: list[Any], budget: ContextBudget, tools=None) -> BudgetSnapshot:
    count, method = estimate_messages(model, messages, tools)
    return BudgetSnapshot(
        count,
        budget.window,
        budget.output,
        budget.safety,
        budget.input_limit,
        budget.high,
        budget.target,
        method,
    )
