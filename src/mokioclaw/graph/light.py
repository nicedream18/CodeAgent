"""An opt-in short path; uncertain or unsuccessful work escalates once."""

from langgraph.graph import END, START, StateGraph

from mokioclaw.agents.registry import run_subagent
from mokioclaw.graph.nodes import _get_writer, final_node, verifier_node
from mokioclaw.graph.state import MokioGraphState


def light_executor(state):
    runtime = state["runtime"]
    runtime.light_active = True
    runtime.light_escalation = False
    working = dict(state)
    runtime.foreground_state = working
    working.setdefault(
        "todos",
        [{"id": "todo-1", "content": state["task"], "status": "pending", "note": ""}],
    )
    working.setdefault("acceptance_criteria", [state["task"]])
    try:
        result = run_subagent(
            working,
            "codeAgent",
            "Complete this small task. Keep changes within one file; report required broader work.",
            writer=_get_writer(),
        )
        return {
            **result.get("state_updates", {}),
            "todos": result.get("state_updates", {}).get("todos", working["todos"]),
            "acceptance_criteria": working["acceptance_criteria"],
            "workflow_escalate": runtime.light_escalation,
        }
    finally:
        runtime.light_active = False


def full_executor(state):
    from mokioclaw.graph.workflow import build_complex_workflow

    runtime = state["runtime"]
    runtime.light_active = False
    runtime.light_target = None
    _get_writer()(
        {
            "type": "workflow_selected",
            "mode": "full",
            "reason": "light path required broader work or verification failed",
            "upgraded": True,
        }
    )
    # A nested invoke consumes its custom stream without forwarding it to the
    # outer graph. Forward incrementally so upgraded runs keep their model/tool
    # events and acknowledge stream endings before lifecycle cleanup.
    from langgraph.config import get_stream_writer

    parent_writer = get_stream_writer()
    result = {**state, "workflow_upgraded": True, "passed": False}
    for mode, event in build_complex_workflow().stream(
        result, stream_mode=["values", "custom"]
    ):
        if mode == "custom":
            parent_writer(event)
        else:
            result = event
    return {key: value for key, value in result.items() if key != "runtime"}


def build_light_workflow():
    graph = StateGraph(MokioGraphState)
    graph.add_node("light_executor", light_executor)
    graph.add_node("verifier", verifier_node)
    graph.add_node("full_executor", full_executor)
    graph.add_node("final", final_node)
    graph.add_edge(START, "light_executor")
    graph.add_conditional_edges(
        "light_executor",
        lambda s: "full_executor" if s.get("workflow_escalate") else "verifier",
    )
    graph.add_conditional_edges(
        "verifier", lambda s: "final" if s.get("passed") else "full_executor"
    )
    graph.add_edge("full_executor", END)
    graph.add_edge("final", END)
    return graph.compile()
