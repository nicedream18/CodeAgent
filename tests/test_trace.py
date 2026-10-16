from __future__ import annotations

import json
from pathlib import Path

from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder


def test_trace_recorder_writes_events_summary_and_timeline(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    trace.start({"task": "demo", "max_attempts": 3})
    trace.record_graph_update({"planner": {"plan_summary": "plan"}})
    trace.record_custom_event(
        {
            "type": "tool_call",
            "node": "codeAgent",
            "name": "BashTool",
            "args": {"command": "python --version"},
        }
    )
    trace.record_custom_event(
        {
            "type": "tool_result",
            "node": "codeAgent",
            "name": "BashTool",
            "result": {"ok": True},
        }
    )
    event = trace.end(status="finished", latest_node="final", final_state={"passed": True})

    assert event is not None
    assert event["type"] == "trace_summary"
    assert runtime.trace_id == trace.trace_id
    assert (trace.root / "events.jsonl").exists()
    assert (trace.root / "summary.json").exists()
    assert (trace.root / "timeline.md").exists()
    summary = json.loads((trace.root / "summary.json").read_text(encoding="utf-8"))
    assert summary["node_visits"] == {"planner": 1}
    assert summary["tool_calls"] == 1


def test_trace_recorder_trims_long_payload(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")
    long_text = "x" * 5000

    trace.start({"task": "demo"})
    trace.record_custom_event({"type": "tool_result", "result": {"ok": True, "stdout": long_text}})

    line = (trace.root / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1]
    payload = json.loads(line)["payload"]
    assert len(payload["payload"]["result"]["stdout"]) < 1300
    assert long_text not in line


def test_trace_recorder_counts_failed_tools_and_approvals(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    trace.record_custom_event({"type": "tool_call", "node": "codeAgent", "name": "BashTool"})
    trace.record_custom_event(
        {
            "type": "tool_result",
            "node": "codeAgent",
            "name": "BashTool",
            "result": {"ok": False, "requires_approval": True, "approved": False},
        }
    )
    event = trace.end(status="interrupted", latest_node="planner")

    assert event is not None
    assert event["tool_calls"] == 1
    assert event["failed_tool_calls"] == 1
    assert event["approval_count"] == 1


def test_trace_records_permission_lifecycle_for_one_tool_call(tmp_path: Path) -> None:
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="demo")
    trace.record_custom_event(
        {
            "type": "permission_requested",
            "tool_call_id": "call-1",
            "tool_name": "FileWriteTool",
        }
    )
    trace.record_custom_event(
        {
            "type": "permission_granted",
            "tool_call_id": "call-1",
            "tool_name": "FileWriteTool",
        }
    )
    trace.record_custom_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-1",
            "name": "FileWriteTool",
            "result": {"ok": True},
        }
    )

    summary = trace.end(status="finished")

    assert summary is not None
    assert summary["approval_count"] == 1
    assert summary["permission_decisions"] == {
        "requested": 1,
        "granted": 1,
        "denied": 0,
    }
    log = (trace.root / "events.jsonl").read_text(encoding="utf-8")
    assert '"call-1"' in log


def test_trace_summarizes_correlated_subagent_handoffs_and_budget(
    tmp_path: Path,
) -> None:
    trace = TraceRecorder(RuntimeState(workspace=tmp_path, trace_mode="on"), task="delegate")
    trace.record_custom_event(
        {
            "type": "handoff",
            "handoff_id": "handoff-1",
            "to": "codeAgent",
            "instruction": "implement",
        }
    )
    trace.record_custom_event(
        {
            "type": "subagent_budget_exhausted",
            "handoff_id": "handoff-1",
            "subagent": "codeAgent",
        }
    )
    trace.record_custom_event(
        {
            "type": "handoff_result",
            "handoff_id": "handoff-1",
            "from": "codeAgent",
            "ok": False,
            "stop_reason": "tool_budget_exhausted",
            "result": "partial",
        }
    )

    summary = trace.end(status="finished")

    assert summary is not None
    assert summary["subagent_budget_exhaustions"] == 1
    assert summary["subagent_result_status"] == {"ok": 0, "failed": 1}
    assert summary["handoffs"] == [
        {
            "handoff_id": "handoff-1",
            "subagent": "codeAgent",
            "instruction": "implement",
            "status": "failed",
            "stop_reason": "tool_budget_exhausted",
        }
    ]


def test_trace_recorder_off_mode_does_not_create_files(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="off")
    trace = TraceRecorder(runtime, task="demo")

    trace.start({"task": "demo"})
    event = trace.end(status="finished")

    assert event is None
    assert not (tmp_path / ".mokioclaw" / "traces").exists()


def test_trace_recorder_write_errors_do_not_raise(tmp_path: Path, monkeypatch) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    def broken_open(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "open", broken_open)
    trace.record_custom_event({"type": "tool_call", "name": "BashTool"})

    assert trace.errors


def test_trace_timeline_keeps_head_and_tail_with_omission(tmp_path: Path) -> None:
    runtime = RuntimeState(workspace=tmp_path, trace_mode="on")
    trace = TraceRecorder(runtime, task="demo")

    for idx in range(140):
        trace.record("custom:test", {"event_type": f"event-{idx}"})
    event = trace.end(status="finished")

    assert event is not None
    assert event["timeline_omitted"] > 0
    timeline = (trace.root / "timeline.md").read_text(encoding="utf-8")
    assert "event-0" in timeline
    assert "event-139" in timeline
    assert "omitted" in timeline
