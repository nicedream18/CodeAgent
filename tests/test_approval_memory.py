from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.approval_rules import ApprovalRules
from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tools


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))


def test_session_scope_reuses_only_same_session(tmp_path):
    cache = {}
    a = ApprovalRules(tmp_path, "s", cache)
    a.remember("FileWriteTool", {"file_path": "a"}, True, "session")
    assert (
        ApprovalRules(tmp_path, "s", cache)
        .match("FileWriteTool", {"file_path": "a", "content": "new"})
        .allowed
    )
    assert (
        ApprovalRules(tmp_path, "other", cache).match("FileWriteTool", {"file_path": "a"}) is None
    )
    assert ApprovalRules(tmp_path, "s", {}).match("FileWriteTool", {"file_path": "a"}) is None
    assert not a.path.exists()


def test_project_rules_are_exact_isolated_and_revocable(tmp_path):
    a = ApprovalRules(tmp_path)
    rule = a.remember("FileWriteTool", {"file_path": "src/a.py"}, True, "project")
    b = ApprovalRules(tmp_path)
    assert b.match("FileWriteTool", {"file_path": "src/a.py"}).id == rule.id
    assert b.match("FileWriteTool", {"file_path": "src/b.py"}) is None
    assert (
        ApprovalRules(tmp_path / "other").match("FileWriteTool", {"file_path": "src/a.py"}) is None
    )
    assert a.path.stat().st_mode & 0o777 == 0o600
    version = a.version()
    assert b.revoke(rule.id)
    assert a.version() != version
    assert not a.list_rules()["rules"]


def test_directory_mcp_fingerprint_and_deny_precedence(tmp_path):
    rules = ApprovalRules(tmp_path, "s", {})
    rules.remember("FileWriteTool", {"file_path": "src/a"}, True, "project", "directory")
    assert rules.match("FileWriteTool", {"file_path": "src/nested/b"}).allowed
    assert rules.match("FileWriteTool", {"file_path": "src2/a"}) is None
    rules.remember("FileWriteTool", {"file_path": "src/a"}, False, "session")
    assert not rules.match("FileWriteTool", {"file_path": "src/a"}).allowed
    server = [{"name": "echo", "command": "python", "args": ["one"]}]
    rules.remember("MCP_echo_run", {"x": 1}, True, "project", servers=server)
    assert rules.match("MCP_echo_run", {"x": 1}, server).allowed
    assert rules.match("MCP_echo_run", {"x": 2}, server) is None
    server[0]["args"] = ["two"]
    assert rules.match("MCP_echo_run", {"x": 1}, server) is None


def test_saved_grant_does_not_bypass_static_policy_or_changed_context(tmp_path):
    runtime = RuntimeState(workspace=tmp_path, approval_mode="inline")
    runtime.tool_policies["FileWriteTool"] = "ask"
    calls = []
    runtime.approval_handler = lambda request: (
        calls.append(request) or ApprovalDecision(True, scope="project")
    )
    tool = next(t for t in build_tools(runtime) if t.name == "FileWriteTool")
    assert execute_tool(runtime, tool, {"file_path": "a", "content": "one"})["ok"]
    from mokioclaw.tools.file_tools import read_file

    read_file(runtime, "a")
    assert execute_tool(runtime, tool, {"file_path": "a", "content": "two"})["ok"]
    assert len(calls) == 1
    runtime.tool_policies["FileWriteTool"] = "deny"
    assert not execute_tool(runtime, tool, {"file_path": "a", "content": "three"})["ok"]
    assert (tmp_path / "a").read_text() == "two"


def test_concurrent_project_saves_do_not_lose_rules(tmp_path):
    def save(i):
        ApprovalRules(tmp_path).remember("FileWriteTool", {"file_path": str(i)}, True, "project")

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(save, range(12)))
    rules = ApprovalRules(tmp_path).list_rules()["rules"]
    assert len(rules) == 12 and len({r["id"] for r in rules}) == 12
    assert len(json.loads(ApprovalRules(tmp_path).path.read_text())["rules"]) == 12


def test_late_second_approval_cannot_reverse_first_decision(tmp_path):
    from mokioclaw.core.approval import ApprovalRequest
    from mokioclaw.core.daemon import AgentDaemon, ApprovalWaiter, RunRecord

    daemon = AgentDaemon(tmp_path / "daemon")
    try:
        record = RunRecord("run-approval", tmp_path / "run")
        waiter = ApprovalWaiter(ApprovalRequest("one", "test", "test"))
        record.waiters["one"] = waiter
        daemon.runs[record.run_id] = record
        params = {"run_id": record.run_id, "approval_id": "one", "approved": False}
        assert daemon.resolve_approval(params)["ok"]
        assert not daemon.resolve_approval({**params, "approved": True})["ok"]
        assert not waiter.decision.approved
    finally:
        daemon.executor.shutdown(wait=True)
