"""Controlled offline full workflow: edits survive a budget drain into verification.

The fixed model is deliberately inefficient; its usage is synthetic. This is a
control-flow experiment, never evidence of real model task-solving performance.
The same driver can be run against B0/B1 by selecting their src with PYTHONPATH.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

import mokioclaw
from mokioclaw.agents import code_agent, repo_agent
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.state import RuntimeState
from mokioclaw.graph import nodes
from mokioclaw.graph.workflow import build_complex_workflow
from mokioclaw.providers.model_policy import RunBudgetExceeded


class Counter:
    mokio_profile = {"context_tokens": 8192, "output_tokens": 128}

    def get_num_tokens_from_messages(self, messages):
        return 100


def reply(content="", calls=None):
    return AIMessage(content=content, tool_calls=calls or [],
                     usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})


class Repo(Counter):
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return self

    def invoke(self, messages):
        self.step += 1
        if self.step == 1:
            return reply(calls=[{"name": "FileReadTool", "args": {"file_path": "a.py"}, "id": "repo-read"}])
        return reply('{"summary":"a.py:1 contains the implementation; edit bad to fixed"}')


class Code(Counter):
    def __init__(self):
        self.step = 0

    def bind_tools(self, tools):
        return BoundCode(self)

    def invoke(self, messages):
        from mokioclaw.core.context_compaction import message_groups

        message_groups(messages)
        return reply("Changed a.py to fixed. Verification remains pending; no test success claimed.")


class BoundCode(Counter):
    def __init__(self, parent):
        self.parent = parent

    def invoke(self, messages):
        self.parent.step += 1
        i = self.parent.step
        name, args = "FileReadTool", {"file_path": "a.py"}
        if i == 2:
            name, args = "FileEditTool", {"file_path": "a.py", "old_text": "bad", "new_text": "fixed"}
        return reply(calls=[{"name": name, "args": args, "id": f"code-{i}"}])


class Coordinator(Counter):
    def __init__(self, passed=True):
        self.passed = passed

    def bind_tools(self, tools):
        planner = any(t.name == "CallCodeAgentTool" for t in tools)
        return BoundCoordinator(planner, self.passed)

    def invoke(self, messages):
        return reply("Coordinator report: inspect actual evidence.")


class BoundCoordinator(Counter):
    def __init__(self, planner, passed):
        self.planner, self.passed = planner, passed
        self.step = 0

    def invoke(self, messages):
        self.step += 1
        if self.planner:
            return reply(calls=[
                {"name": "CallCodeAgentTool", "args": {"instruction": "Fix a.py"}, "id": "delegate"},
                {"name": "CallCodeAgentTool", "args": {"instruction": "This obsolete handoff must not start after drain"}, "id": "obsolete"},
            ])
        if self.step == 1:
            return reply(calls=[{"name": "FileReadTool", "args": {"file_path": "a.py"}, "id": "verify-read"}])
        observed = any(isinstance(m, ToolMessage) and "fixed" in str(m.content) for m in messages)
        passed = self.passed and observed
        return reply(json.dumps({"passed": passed, "reason": "Observed file" if passed else "Evidence insufficient",
                                 "checks": [{"name": "read a.py", "passed": passed}],
                                 "recommended_next_instruction": ""}))


def run_demo(kind="requests", *, passed=True, on_event=None):
    events = []
    control = RunControl()
    with TemporaryDirectory(prefix="mokio-completion-demo-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "a.py").write_text("bad\n")
        runtime = RuntimeState(workspace=workspace, approval_mode="auto", run_control=control,
                               run_request_limit=10 if kind == "requests" else 40,
                               run_token_limit=1800 if kind == "tokens" else 0,
                               run_tool_limit=7 if kind == "tools" else 80,
                               context_token_limit=8192, model_context_reserve_tokens=128)
        state = {"task": "Change bad to fixed in a.py; preserve other files", "runtime": runtime,
                 "max_attempts": 3}

        def writer(event):
            events.append(event)
            if on_event:
                on_event(event, runtime)

        try:
            with patch.dict(os.environ, {"HOME": str(root / "home"), "PYTHON_DOTENV_DISABLED": "1"}), \
                 patch.object(nodes, "_get_writer", lambda: writer), \
                 patch.object(nodes, "create_model", lambda: Coordinator(passed)), \
                 patch.object(code_agent, "create_model", Code), \
                 patch.object(repo_agent, "create_model", Repo):
                try:
                    final = build_complex_workflow().invoke(state, {"recursion_limit": 100})
                    status, stop_reason = "finished", None
                    from mokioclaw.core.context_compaction import message_groups

                    message_groups(final.get("messages", []))
                except RunBudgetExceeded as exc:
                    final = exc.state
                    status, stop_reason = "failed", exc.reason
            return {
                "real_model": False, "kind": kind, "execution_status": status,
                "implementation_source": str(Path(mokioclaw.__file__).resolve().parent),
                "stop_reason": stop_reason, "passed": final.get("passed", False),
                "file_content": (workspace / "a.py").read_text(),
                "workspace_files": [p.relative_to(workspace).as_posix() for p in workspace.rglob("*")
                                    if p.is_file() and ".mokioclaw" not in p.parts],
                "requests": runtime.model_metrics.get("requests", 0),
                "total_tokens": runtime.model_metrics.get("total_tokens", 0),
                "tools": runtime.tool_execution_count,
                "roles": runtime.model_metrics.get("roles", {}),
                "events": events,
                "completion_budget": runtime.completion_budget.snapshot()
                if getattr(runtime, "completion_budget", None) else {},
            }
        finally:
            control.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["requests", "tokens", "tools"], default="requests")
    parser.add_argument("--fail-verification", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_demo(args.kind, passed=not args.fail_verification)
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in {"events", "roles"}}, ensure_ascii=False))
