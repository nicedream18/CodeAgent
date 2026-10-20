"""Bounded synchronous event bridge around real CrewAI Flow execution.

RuntimeState (locks, approvals and model clients) stays outside CrewAI's
serializable cursor. A node completion is acknowledged by the consumer before
the next Flow method may start, preserving correction/cancellation boundaries.
"""

from __future__ import annotations

import contextvars
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from crewai.flow.flow import Flow, start
from pydantic import BaseModel, PrivateAttr

from mokioclaw.core.run_control import check_run
from mokioclaw.orchestration.messages import add_messages

State = dict[str, Any]
Node = Callable[[Any], dict[str, Any]]
Writer = Callable[[dict[str, Any]], None]
_WRITER: contextvars.ContextVar[Writer | None] = contextvars.ContextVar(
    "mokio_flow_writer", default=None
)


def get_stream_writer() -> Writer:
    writer = _WRITER.get()
    if writer is None:
        raise RuntimeError("No active MokioAgent Flow stream")
    return writer


class WorkflowLimitError(RuntimeError):
    """The configured node budget was exhausted without reaching a terminal."""


class _StreamClosed(RuntimeError):
    pass


class FlowCursor(BaseModel):
    next_node: str = ""
    completed_node: str = ""
    steps: int = 0


@dataclass
class _Item:
    kind: str
    value: Any
    acknowledged: threading.Event | None = None


@dataclass
class _Execution:
    payload: State
    nodes: dict[str, Node]
    modes: set[str]
    limit: int
    initial_node: str = ""
    events: queue.Queue[_Item] = field(default_factory=lambda: queue.Queue(maxsize=128))
    closed: threading.Event = field(default_factory=threading.Event)
    steps: int = 0

    def send(self, item: _Item) -> None:
        while not self.closed.is_set():
            try:
                self.events.put(item, timeout=0.05)
                return
            except queue.Full:
                continue
        raise _StreamClosed()

    def write(self, event: dict[str, Any]) -> None:
        if self.closed.is_set():
            raise _StreamClosed()
        if "custom" in self.modes:
            self.send(_Item("custom", event))

    def step(self, name: str) -> None:
        if self.closed.is_set():
            raise _StreamClosed()
        check_run(self.payload.get("runtime"))
        if self.steps >= self.limit:
            raise WorkflowLimitError(f"CrewAI Flow node limit ({self.limit}) exceeded at {name}")
        self.steps += 1
        update = self.nodes[name](dict(self.payload))
        check_run(self.payload.get("runtime"))
        if not isinstance(update, dict):
            raise TypeError(f"Flow node {name} must return a state update")
        for key, value in update.items():
            self.payload[key] = (
                add_messages(self.payload.get(key, []), value) if key == "messages" else value
            )
        emitted = []
        if "updates" in self.modes:
            emitted.append(_Item("updates", {name: update}))
        if "values" in self.modes:
            emitted.append(_Item("values", dict(self.payload)))
        if emitted:
            acknowledgement = threading.Event()
            emitted[-1].acknowledged = acknowledgement
            for item in emitted:
                self.send(item)
            while not acknowledgement.wait(0.05):
                if self.closed.is_set():
                    raise _StreamClosed()


class ControlledFlow(Flow[FlowCursor]):
    # CrewAI 1.15.28 creates embedding memory by default. MokioAgent owns memory
    # and must not create another store, embedding request or implicit LLM call.
    _skip_auto_memory: ClassVar[bool] = True
    _execution: _Execution | None = PrivateAttr(default=None)

    def run_step(self, name: str) -> None:
        if self._execution is None:
            raise RuntimeError("Flow has no execution session")
        self._execution.step(name)
        self.state.completed_node = name
        self.state.steps = self._execution.steps

    @property
    def payload(self) -> State:
        if self._execution is None:
            raise RuntimeError("Flow has no execution session")
        return self._execution.payload

    def _log_flow_event(
        self, message: str, color: str = "yellow", level: Literal["info", "warning"] = "info"
    ) -> None:
        # Framework console output would corrupt RPC NDJSON and TUI rendering.
        pass

    @staticmethod
    def _show_tracing_disabled_message() -> None:
        pass


class SingleStepFlow(ControlledFlow):
    @start()
    def execute(self) -> None:
        if self._execution is None:
            raise RuntimeError("Flow has no execution session")
        self.run_step(self._execution.initial_node)


class CrewWorkflow:
    """Small compatibility surface; CrewAI schedules every actual node."""

    backend = "crewai"

    def __init__(
        self, flow_type: type[ControlledFlow], nodes: dict[str, Node], *, initial_node: str = ""
    ) -> None:
        self.flow_type, self.nodes, self.initial_node = flow_type, nodes, initial_node

    def stream(
        self,
        inputs: State,
        config: dict[str, Any] | None = None,
        *,
        stream_mode: str | list[str] = "updates",
    ) -> Iterator[Any]:
        modes = [stream_mode] if isinstance(stream_mode, str) else list(stream_mode)
        if not modes or set(modes) - {"updates", "values", "custom"}:
            raise ValueError("Flow stream modes must be updates, values or custom")
        limit = (config or {}).get("recursion_limit", 25)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("Flow node limit must be a positive integer")
        execution = _Execution(dict(inputs), self.nodes, set(modes), limit, self.initial_node)
        context = contextvars.copy_context()

        def produce() -> None:
            token = _WRITER.set(execution.write)
            try:
                flow = self.flow_type(
                    tracing=False, suppress_flow_events=True, max_method_calls=10000
                )
                flow._execution = execution
                flow.kickoff()
            except _StreamClosed:
                pass
            except BaseException as exc:
                if not execution.closed.is_set():
                    try:
                        execution.send(_Item("error", exc))
                    except _StreamClosed:
                        pass
            finally:
                _WRITER.reset(token)
                if not execution.closed.is_set():
                    try:
                        execution.send(_Item("done", None))
                    except _StreamClosed:
                        pass

        producer = threading.Thread(
            target=lambda: context.run(produce), name="mokio-crewai-flow", daemon=True
        )
        producer.start()
        acknowledgement = None
        try:
            while True:
                if acknowledgement is not None:
                    acknowledgement.set()
                    acknowledgement = None
                try:
                    item = execution.events.get(timeout=0.05)
                except queue.Empty:
                    check_run(inputs.get("runtime"))
                    continue
                if item.kind == "error":
                    raise item.value
                if item.kind == "done":
                    return
                acknowledgement = item.acknowledged
                yield item.value if isinstance(stream_mode, str) else (item.kind, item.value)
        finally:
            execution.closed.set()
            if acknowledgement is not None:
                acknowledgement.set()
            # Started synchronous tools retain their timeout/side-effect policy.
            # Join before lifecycle finalization so no node can emit afterward.
            producer.join()

    def invoke(self, inputs: State, config: dict[str, Any] | None = None) -> State:
        result = dict(inputs)
        for value in self.stream(inputs, config, stream_mode="values"):
            result = value
        return result


def single_step_workflow(name: str, node: Node) -> CrewWorkflow:
    return CrewWorkflow(SingleStepFlow, {name: node}, initial_node=name)
