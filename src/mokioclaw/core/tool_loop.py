"""Shared model/tool round boundaries for all controlled runners.

The caller supplies prompts, tools, result conversion and an optional dynamic
binding callback. Cancellation, corrections, budgets and discarded calls keep
using the same provider and batch boundary in every role.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

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
        runtime = self.state.get("runtime") if self.state else None
        if runtime is not None:
            marker = "MOKIO EXECUTION BUDGET\n"
            facts = []
            active = runtime.active_subagent_budget
            if active is not None:
                facts.append(
                    f"Role tool calls remaining: {max(0, active.limit - active.used)} of {active.limit}."
                )
            if runtime.run_request_limit:
                facts.append(
                    f"Run model requests remaining: {max(0, runtime.run_request_limit - runtime.model_metrics.get('requests', 0))}."
                )
            if runtime.run_token_limit:
                facts.append(
                    f"Run token headroom before the next reservation: {max(0, runtime.run_token_limit - runtime.model_metrics.get('total_tokens', 0))} (estimated when usage is missing)."
                )
            self.messages[:] = [
                m
                for m in self.messages
                if not (isinstance(m, SystemMessage) and str(m.content).startswith(marker))
            ]
            if facts:
                self.messages.insert(
                    1,
                    SystemMessage(
                        content=marker
                        + "\n".join(facts)
                        + "\nUse existing repository evidence. Read focused source ranges, implement the requested change, "
                        "then run relevant checks and report. Each TODO/read/search/check consumes a tool call. "
                        "Avoid broad discovery after the target is known; preserve budget for verification and the final report."
                    ),
                )
        response = self.invoke(
            runnable, self.messages, writer=self.writer, node=self.node, state=self.state
        )
        self.append(response)
        return response

    def append(self, message: Any) -> None:
        self.messages.append(message)
        if self.produced is not None:
            self.produced.append(message)

    def discard_unanswered(self, calls: Any, reason: str) -> None:
        answered = {m.tool_call_id for m in self.messages if isinstance(m, ToolMessage)}
        for call in calls:
            if call["id"] in answered:
                continue
            result = {"ok": False, "status": "not_executed", "error_type": reason}
            self.append(
                ToolMessage(content=json.dumps(result), name=call["name"], tool_call_id=call["id"])
            )
            self.writer(
                {
                    "type": "tool_result",
                    "node": self.node,
                    "name": call["name"],
                    "tool_call_id": call["id"],
                    "result": result,
                }
            )

    def finish(self, model: Any, instruction: str) -> Any:
        """Request one report without binding tools; never execute final calls."""
        self.append(HumanMessage(content=instruction))
        response = self.request(model)
        calls = getattr(response, "tool_calls", None) or []
        if calls:
            self.discard_unanswered(calls, "finalization_tools_not_allowed")
            return None
        return response

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
