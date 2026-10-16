from __future__ import annotations

import json
from io import BytesIO
import threading
import time
from pathlib import Path

import pytest

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.daemon import AgentDaemon
from mokioclaw.core.daemon_client import daemon_status, run_via_daemon, stop_daemon


def _start_daemon(state_dir: Path) -> tuple[AgentDaemon, threading.Thread]:
    daemon = AgentDaemon(state_dir, port=0)
    thread = threading.Thread(target=daemon.run, daemon=True)
    thread.start()
    for _ in range(200):
        if (state_dir / "address.json").exists():
            return daemon, thread
        time.sleep(0.01)
    raise AssertionError("daemon did not publish its address")


def test_daemon_requires_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AgentDaemon(Path("/tmp/unsafe-mokioclaw-daemon"), host="0.0.0.0")


def test_daemon_runs_task_streams_events_and_roundtrips_approval(
    monkeypatch, tmp_path: Path
) -> None:
    import mokioclaw.core.agent as agent

    state_dir = tmp_path / "daemon"
    monkeypatch.setenv("MOKIO_DAEMON_DIR", str(state_dir))
    monkeypatch.setenv("MOKIO_DAEMON_WORKER", "")
    daemon, thread = _start_daemon(state_dir)

    def fake_stream(task, *, approval_handler=None, **kwargs):
        assert task == "create a page"
        decision = approval_handler(
            ApprovalRequest("approval-demo", "write page", "file write")
        )
        yield {"type": "workspace", "path": "/tmp/workspace"}
        yield {
            "type": "custom_event",
            "event": {"type": "approval_result", "approved": decision.approved},
        }

    monkeypatch.setattr(agent, "stream_agent_events", fake_stream)
    approvals = []
    try:
        events = list(
            run_via_daemon(
                "run",
                "create a page",
                options={"approval_mode": "inline"},
                approval_handler=lambda request: (
                    approvals.append(request) or ApprovalDecision(True)
                ),
            )
        )
        assert [event.get("type") for event in events] == ["custom_event", "workspace", "custom_event"]
        assert events[0]["event"]["type"] == "run_started"
        assert events[-1]["event"]["approved"] is True
        assert approvals and approvals[0].id == "approval-demo"
        status = daemon_status(start=False)
        assert status and len(status["runs"]) == 1
        run_dir = next((state_dir / "runs").iterdir())
        persisted = [
            json.loads(line)
            for line in (run_dir / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert any(
            item["event"].get("type") == "daemon_approval_request" for item in persisted
        )
        assert [item["seq"] for item in persisted] == list(range(1, len(persisted) + 1))
    finally:
        stop_daemon()
        thread.join(timeout=3)
    assert not thread.is_alive()


def test_daemon_run_log_is_reopenable(tmp_path: Path) -> None:
    from mokioclaw.core.daemon import RunRecord

    record = RunRecord("run-test", tmp_path / "run-test")
    record.append({"type": "first"})
    restored = RunRecord("run-test", tmp_path / "run-test")
    assert restored.events[0]["event"]["type"] == "first"
    assert restored.events[0]["seq"] == 1


def test_daemon_subscription_resumes_after_sequence(tmp_path: Path) -> None:
    daemon = AgentDaemon(tmp_path / "daemon")
    from mokioclaw.core.daemon import RunRecord

    run = RunRecord("run-resume", tmp_path / "run-resume")
    run.append({"type": "first"})
    run.append({"type": "second"})
    run.status = "finished"
    daemon.runs[run.run_id] = run
    output = BytesIO()
    response = daemon.dispatch(
        {
            "jsonrpc": "2.0",
            "id": "subscribe-1",
            "method": "subscribe",
            "params": {"token": daemon.token, "run_id": run.run_id, "after_seq": 1},
        },
        output,
    )
    assert response is None
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    event_messages = [item for item in messages if item.get("method") == "event"]
    assert [item["params"]["seq"] for item in event_messages] == [2]
    assert messages[-1]["method"] == "stream_end"


def test_daemon_rejects_approval_response_for_different_tool_call(
    tmp_path: Path,
) -> None:
    from mokioclaw.core.daemon import ApprovalWaiter, RunRecord

    daemon = AgentDaemon(tmp_path / "daemon")
    run = RunRecord("run-approval", tmp_path / "run-approval")
    request = ApprovalRequest(
        "approval-1", "file_path=page.html", "write", "FileWriteTool", "call-expected"
    )
    run.waiters[request.id] = ApprovalWaiter(request)
    daemon.runs[run.run_id] = run

    mismatch = daemon.resolve_approval(
        {
            "run_id": run.run_id,
            "approval_id": request.id,
            "tool_call_id": "call-other",
            "approved": True,
        }
    )
    assert mismatch["ok"] is False
    assert not run.waiters[request.id].event.is_set()

    accepted = daemon.resolve_approval(
        {
            "run_id": run.run_id,
            "approval_id": request.id,
            "tool_call_id": "call-expected",
            "approved": True,
        }
    )
    assert accepted["ok"] is True


def test_daemon_disconnect_denies_pending_approval(tmp_path: Path, monkeypatch) -> None:
    from mokioclaw.core.daemon import ApprovalWaiter, RunRecord

    daemon = AgentDaemon(tmp_path / "daemon")
    run = RunRecord("run-disconnect", tmp_path / "run-disconnect")
    request = ApprovalRequest(
        "approval-disconnect", "command=deploy", "deploy", "BashTool", "call-deploy"
    )
    waiter = ApprovalWaiter(request)
    run.waiters[request.id] = waiter
    daemon.runs[run.run_id] = run
    monkeypatch.setattr(run.condition, "wait", lambda timeout: None)

    class ClientDisconnect:
        writes = 0

        def write(self, _data):
            self.writes += 1
            if self.writes > 1:
                raise BrokenPipeError("client disconnected")

        def flush(self):
            pass

    daemon.subscribe("subscribe-1", {"run_id": run.run_id}, ClientDisconnect())

    assert waiter.event.is_set()
    assert waiter.decision == ApprovalDecision(False, "Approval client disconnected.")


def test_daemon_approval_timeout_denies_pending_call(
    monkeypatch, tmp_path: Path
) -> None:
    import mokioclaw.core.agent as agent
    from mokioclaw.core.daemon import RunRecord

    monkeypatch.setenv("MOKIO_APPROVAL_TIMEOUT_SECONDS", "1")
    monkeypatch.setattr(threading.Event, "wait", lambda _self, timeout=None: False)

    def fake_stream(_task, *, approval_handler=None, **_kwargs):
        decision = approval_handler(
            ApprovalRequest(
                "approval-timeout",
                "write",
                "file write",
                "FileWriteTool",
                "call-timeout",
            )
        )
        yield {
            "type": "approval_result",
            "approved": decision.approved,
            "reason": decision.reason,
        }

    monkeypatch.setattr(agent, "stream_agent_events", fake_stream)
    daemon = AgentDaemon(tmp_path / "daemon")
    record = RunRecord("run-timeout", tmp_path / "run-timeout")

    daemon._run_task(record, "run", {"approval_mode": "inline", "task": "write a page"})

    approval_result = next(
        item["event"]
        for item in record.events
        if item["event"].get("type") == "approval_result"
    )
    assert approval_result["approved"] is False
    assert approval_result["reason"] == "Approval request timed out."
