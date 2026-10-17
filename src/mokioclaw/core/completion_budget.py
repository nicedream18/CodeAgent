"""Cooperative implementation drain; all requests retain the existing hard caps."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class CompletionYield(BaseException):
    """Yield to completion, never a retryable model/tool failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.state: dict[str, Any] = {}


@dataclass
class CompletionBudget:
    phase: str = "implementation"
    draining: bool = False
    input_estimate: int = 0
    output_budget: int = 0
    reserved_requests: int = 4  # One specialist report and three verifier requests.
    reserved_tools: int = 2  # Initial verifier read and check; not extra allowance.
    trigger: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "phase": self.phase, "draining": self.draining, "trigger": self.trigger,
            "reserved_requests": self.reserved_requests, "reserved_tools": self.reserved_tools,
            "input_estimate": self.input_estimate, "output_budget": self.output_budget,
            "reserved_tokens_estimate": self.reserved_requests * (self.input_estimate + self.output_budget),
            "estimated": True,
            "forecast": "current request input and output budget reused for future requests",
        }

    def _yield(self, runtime: Any, reason: str, writer: Callable[[dict[str, Any]], None] | None) -> None:
        first = not self.draining
        self.draining = True
        self.trigger = self.trigger or reason
        if first and writer:
            writer({
                "type": "completion_budget", "status": "draining", **self.snapshot(),
                "node": runtime.active_agent_name or "planner",
                "used_requests": runtime.model_metrics.get("requests", 0),
                "used_tokens": runtime.model_metrics.get("total_tokens", 0),
                "used_tools": runtime.tool_execution_count,
                "request_limit": runtime.run_request_limit,
                "token_limit": runtime.run_token_limit,
                "tool_limit": runtime.run_tool_limit,
            })
        raise CompletionYield(reason)

    def before_request(self, runtime: Any, role: str, incoming: int, outgoing: int,
                       writer: Callable[[dict[str, Any]], None] | None, node: str = "") -> None:
        # Compaction during completion uses the existing deterministic fallback,
        # leaving model capacity for the report and actual verification.
        if (node or role) == "context_compressor" and self.draining:
            raise CompletionYield("completion_uses_deterministic_compaction")
        if self.phase != "implementation":
            return
        self.input_estimate, self.output_budget = incoming, outgoing
        request = incoming + outgoing
        tokens = runtime.model_metrics.get("total_tokens", 0)
        requests = runtime.model_metrics.get("requests", 0)
        if self.draining:
            self._yield(runtime, self.trigger, writer)
        if runtime.run_request_limit and requests + 1 + self.reserved_requests > runtime.run_request_limit:
            self._yield(runtime, "request_headroom", writer)
        if runtime.run_token_limit and tokens + request * (1 + self.reserved_requests) > runtime.run_token_limit:
            self._yield(runtime, "token_headroom", writer)

    def before_tool(self, runtime: Any, writer: Callable[[dict[str, Any]], None] | None) -> None:
        if self.phase != "implementation":
            return
        if self.draining:
            self._yield(runtime, self.trigger, writer)
        if runtime.run_tool_limit and runtime.tool_execution_count + 1 + self.reserved_tools > runtime.run_tool_limit:
            self._yield(runtime, "tool_headroom", writer)


def activate_completion_budget(runtime: Any) -> CompletionBudget | None:
    if not any((runtime.run_token_limit, runtime.run_request_limit, runtime.run_tool_limit)):
        return None
    control = getattr(runtime, "completion_budget", None)
    if control is None:
        control = CompletionBudget()
        runtime.completion_budget = control
    # A new planning pass (including accepted corrections) re-evaluates the
    # remaining hard budget. A drained failed verification never loops here.
    control.phase = "implementation"
    control.draining = False
    control.trigger = ""
    return control


@contextmanager
def report_phase(runtime: Any) -> Iterator[None]:
    control = getattr(runtime, "completion_budget", None)
    previous = control.phase if control else None
    if control:
        control.phase = "report"
    try:
        yield
    finally:
        if control:
            control.phase = previous
