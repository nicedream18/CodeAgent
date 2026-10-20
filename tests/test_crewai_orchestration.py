"""Real CrewAI scheduling with local deterministic nodes; no model requests."""

from __future__ import annotations

import contextvars
import json
import threading
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage

from mokioclaw.graph import workflow
from mokioclaw.orchestration.flows import EntryFlow, FullFlow
from mokioclaw.orchestration.messages import REMOVE_ALL_MESSAGES, add_messages
from mokioclaw.orchestration.runtime import (
    SingleStepFlow,
    WorkflowLimitError,
    get_stream_writer,
    single_step_workflow,
)


def test_full_flow_replans_and_reaches_final_without_extra_calls(monkeypatch):
    visited = []

    def node(name, update):
        def run(state):
            visited.append(name)
            return update(state)

        return run

    monkeypatch.setattr(workflow, "planner_node", node("planner", lambda _: {}))
    monkeypatch.setattr(
        workflow,
        "context_monitor_node",
        node(
            "monitor",
            lambda state: {
                "context_next_node": "final"
                if state.get("passed")
                else "planner"
                if state.get("attempts") == 1
                else "verifier",
                "context_should_compress": False,
            },
        ),
    )
    monkeypatch.setattr(
        workflow,
        "verifier_node",
        node(
            "verifier",
            lambda state: {
                "attempts": state.get("attempts", 0) + 1,
                "passed": state.get("attempts", 0) == 1,
            },
        ),
    )
    monkeypatch.setattr(workflow, "final_node", node("final", lambda _: {"final_answer": "done"}))

    # After replanning, the monitor must permit verification rather than loop.
    def planner(state):
        visited.append("planner")
        return {"attempts": state.get("attempts", 0), "replanned": bool(state.get("attempts"))}

    monkeypatch.setattr(workflow, "planner_node", planner)
    monkeypatch.setattr(
        workflow,
        "context_monitor_node",
        node(
            "monitor",
            lambda state: {
                "context_next_node": "final"
                if state.get("passed")
                else "planner"
                if state.get("attempts") == 1 and not state.get("replanned")
                else "verifier",
                "context_should_compress": False,
            },
        ),
    )
    flow = workflow.build_complex_workflow()
    assert flow.flow_type is FullFlow and flow.backend == "crewai"
    result = flow.invoke({"task": "local", "attempts": 0})
    assert result["final_answer"] == "done" and result["attempts"] == 2
    assert visited == [
        "planner",
        "monitor",
        "verifier",
        "monitor",
        "planner",
        "monitor",
        "verifier",
        "monitor",
        "final",
    ]


@pytest.mark.parametrize("route", ["chat", "workflow"])
def test_entry_flow_routes_without_a_second_model_request(monkeypatch, route):
    called = []
    monkeypatch.setattr(workflow, "intent_router_node", lambda _: {"intent_route": route})
    monkeypatch.setattr(
        workflow,
        "chat_responder_node",
        lambda _: (
            called.append("chat")
            or {
                "final_answer": "hello",
            }
        ),
    )
    entry = workflow.build_entry_workflow()
    assert entry.flow_type is EntryFlow
    result = entry.invoke({"task": "hello"})
    assert called == (["chat"] if route == "chat" else [])
    assert result["intent_route"] == route


def test_custom_events_are_live_and_context_propagates_to_flow_thread():
    marker = contextvars.ContextVar("test_marker", default="lost")
    release = threading.Event()
    completed = threading.Event()

    def step(_):
        get_stream_writer()({"type": "partial", "marker": marker.get()})
        assert release.wait(2)
        completed.set()
        return {"final_answer": "done"}

    token = marker.set("preserved")
    iterator = single_step_workflow("step", step).stream({}, stream_mode=["custom", "updates"])
    try:
        assert next(iterator) == ("custom", {"type": "partial", "marker": "preserved"})
        assert not completed.is_set()
        release.set()
        assert list(iterator) == [("updates", {"step": {"final_answer": "done"}})]
    finally:
        release.set()
        iterator.close()
        marker.reset(token)


def test_closing_after_update_prevents_next_flow_node(monkeypatch):
    visited = []
    monkeypatch.setattr(workflow, "planner_node", lambda _: visited.append("planner") or {})
    monkeypatch.setattr(workflow, "context_monitor_node", lambda _: visited.append("monitor") or {})
    iterator = workflow.build_complex_workflow().stream({})
    assert next(iterator) == {"planner": {}}
    iterator.close()
    assert visited == ["planner"]
    assert not any(t.name == "mokio-crewai-flow" for t in threading.enumerate())


def test_partial_events_survive_and_original_exception_propagates():
    failure = ValueError("controlled failure")

    def step(_):
        get_stream_writer()({"type": "partial"})
        raise failure

    iterator = single_step_workflow("step", step).stream({}, stream_mode=["custom"])
    assert next(iterator) == ("custom", {"type": "partial"})
    with pytest.raises(ValueError) as caught:
        next(iterator)
    assert caught.value is failure


def test_flow_node_limit_stops_without_starting_next_operation(monkeypatch):
    called = []
    monkeypatch.setattr(workflow, "planner_node", lambda _: called.append("planner") or {})
    with pytest.raises(WorkflowLimitError):
        workflow.build_complex_workflow().invoke({}, {"recursion_limit": 1})
    assert called == ["planner"]


def test_cursor_does_not_serialize_runtime_or_create_embedding_memory(capsys):
    flow = SingleStepFlow(tracing=False, suppress_flow_events=True)
    assert flow.memory is None and flow.persistence is None and flow.tracing is False
    state = json.loads(flow.state.model_dump_json())
    assert set(state) == {"id", "next_node", "completed_node", "steps"}
    single_step_workflow("step", lambda _: {"final_answer": "ok"}).invoke({})
    assert not capsys.readouterr().out


def test_message_updates_preserve_checkpoint_ids_and_compression_reset():
    old = HumanMessage(content="old", id="old")
    replacement = AIMessage(content="replacement", id="answer")
    merged = add_messages([old, AIMessage(content="initial", id="answer")], [replacement])
    assert merged == [old, replacement]
    assert add_messages(merged, [RemoveMessage(id="old")]) == [replacement]
    assert add_messages(merged, [RemoveMessage(id=REMOVE_ALL_MESSAGES), replacement]) == [
        replacement
    ]
    with pytest.raises(ValueError, match="unknown message"):
        add_messages(merged, [RemoveMessage(id="missing")])


def test_production_orchestration_has_no_langgraph_imports():
    import ast

    root = Path(__file__).resolve().parents[1] / "src/mokioclaw"
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith("langgraph"), path
            elif isinstance(node, ast.Import):
                assert not any(n.name.startswith("langgraph") for n in node.names), path


def test_light_upgrade_preserves_compacted_nested_history(monkeypatch, tmp_path):
    from mokioclaw.core.state import RuntimeState
    from mokioclaw.graph import light

    monkeypatch.setattr(light, "light_executor", lambda _: {"workflow_escalate": True})
    monkeypatch.setattr(
        workflow,
        "build_complex_workflow",
        lambda: single_step_workflow(
            "compacted",
            lambda _: {
                "messages": [
                    RemoveMessage(id=REMOVE_ALL_MESSAGES),
                    AIMessage(content="summary", id="new"),
                ],
                "final_answer": "done",
            },
        ),
    )
    result = light.build_light_workflow().invoke(
        {
            "runtime": RuntimeState(workspace=tmp_path),
            "messages": [HumanMessage(content="old history", id="old")],
        }
    )
    assert [m.id for m in result["messages"]] == ["new"]
    assert result["final_answer"] == "done" and result["workflow_upgraded"]
