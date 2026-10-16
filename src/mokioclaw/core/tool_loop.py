"""Shared model/tool round boundaries for all controlled runners.

The caller supplies prompts, tools, result conversion and an optional dynamic
binding callback. Cancellation, corrections, budgets and discarded calls keep
using the same provider and batch boundary in every role.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from mokioclaw.core.run_inbox import tool_call_batch


class ToolConversation:
    def __init__(
        self,
        messages: list[Any],
        *,
        state: Any,
        writer: Any,
        node: str,
        invoke: Callable[..., Any],
        produced: list[Any] | None = None,
    ):
        self.messages = messages
        self.state, self.writer, self.node, self.invoke = state, writer, node, invoke
        self.produced = produced

    def request(self, runnable: Any) -> Any:
        response = self.invoke(
            runnable, self.messages, writer=self.writer, node=self.node, state=self.state
        )
        self.append(response)
        return response

    def append(self, message: Any) -> None:
        self.messages.append(message)
        if self.produced is not None:
            self.produced.append(message)

    def rounds(self, count: int, binding: Callable[[], Any]) -> Iterator[tuple[Any, Any]]:
        for _ in range(count):
            response = self.request(binding())
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                yield response, []
                return
            yield response, calls

    def batch(self, calls: Any) -> AbstractContextManager[Any]:
        # Keep this context in the caller's lexical scope so exceptions from a
        # specialist reach the batch boundary before the generator is closed.
        return tool_call_batch(calls, self.messages, self.writer, self.node)
