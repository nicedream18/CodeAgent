"""An opt-in short path; uncertain or unsuccessful work escalates once."""

from mokioclaw.agents.registry import run_subagent
from mokioclaw.graph.nodes import _get_writer, final_node, test_agent_node, verifier_node
from mokioclaw.orchestration.flows import LightFlow
from mokioclaw.orchestration.runtime import CrewWorkflow, get_stream_writer


def light_executor(state):
    runtime = state["runtime"]
    runtime.light_active = True
    runtime.light_escalation = False
    working = {**state, "testing_required": True, "test_report": {}}
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
            "testing_required": True,
            "test_report": {},
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
    parent_writer = get_stream_writer()
    result = {**state, "workflow_upgraded": True, "passed": False}
    for mode, event in build_complex_workflow().stream(
        result, stream_mode=["values", "custom"]
    ):
        if mode == "custom":
            parent_writer(event)
        else:
            result = event
    if "messages" in result:
        from langchain_core.messages import RemoveMessage

        from mokioclaw.orchestration.messages import REMOVE_ALL_MESSAGES

        result["messages"] = [RemoveMessage(id=REMOVE_ALL_MESSAGES), *result["messages"]]
    return {key: value for key, value in result.items() if key != "runtime"}


def build_light_workflow():
    return CrewWorkflow(LightFlow, {
        "light_executor": light_executor,
        "verifier": verifier_node,
        "testAgent": test_agent_node,
        "full_executor": full_executor,
        "final": final_node,
    })
