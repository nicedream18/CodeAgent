from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolCallContext:
    tool_call_id: str
    tool_name: str
    permission_checked: bool
    event_writer: Callable[[dict[str, Any]], None] | None = None


_CURRENT_TOOL_CALL: ContextVar[ToolCallContext | None] = ContextVar(
    "mokioclaw_tool_call", default=None
)


def set_tool_call_context(context: ToolCallContext) -> Token:
    return _CURRENT_TOOL_CALL.set(context)


def reset_tool_call_context(token: Token) -> None:
    _CURRENT_TOOL_CALL.reset(token)


def current_tool_call_context() -> ToolCallContext | None:
    return _CURRENT_TOOL_CALL.get()
