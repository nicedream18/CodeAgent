"""Automatic testing handoff between implementation and final model review."""

from mokioclaw.graph import nodes as api


def test_agent_node(state):
    runtime = state["runtime"]
    api.check_run(runtime)
    runtime.foreground_state = state
    writer = api._get_writer()
    working = dict(state)
    completion = runtime.completion_budget
    if completion and completion.draining:
        # Preserve the already-reserved final review capacity. No hidden model
        # request or synthetic testing success when implementation exhausted it.
        return {
            "test_report": {
                "status": "not_run",
                "commands": [],
                "summary": "Testing skipped to preserve reserved final review capacity.",
                "stop_reason": "completion_budget_reserved",
                "basis": "observed_tool_results",
            },
            "testing_required": False,
            "context_next_node": "verifier",
        }
    previous = completion.phase if completion else None
    if completion:
        # Test execution must leave the existing reserved headroom for verifier.
        completion.phase = "implementation"
    try:
        result = api._call_registered_subagent_tool(
            working,
            writer,
            "testAgent",
            "Independently inspect the implementation and run the relevant tests. "
            "Report observed command outcomes and missing tests; delegate repairs to codeAgent.",
        )
    finally:
        if completion:
            completion.phase = previous
    report = working.get("test_report") or {
        "status": "incomplete",
        "summary": result.get("summary", ""),
        "commands": [],
        "errors": result.get("unresolved", []),
        "basis": "observed_tool_results",
    }
    if not result.get("ok") and report.get("status") in {"passed", "not_run"}:
        report = {**report, "status": "incomplete", "stop_reason": result.get("stop_reason")}
    return {
        "test_report": report,
        "testing_required": False,
        "agent_handoffs": working.get("agent_handoffs", []),
        "context_next_node": "verifier",
    }
