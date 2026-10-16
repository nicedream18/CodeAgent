"""Reproducible optimization experiments; network only with --real-model."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from optimization_models import CrossModuleModel, WorkflowModel

from mokioclaw.core.agent import _graph_envelopes, stream_agent_events
from mokioclaw.core.changes import rewind
from mokioclaw.core.instructions import inject_instructions
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.run_inbox import RunInbox
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.graph.state import MokioGraphState
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.file_tools import edit_file, read_file
from mokioclaw.tools.grep_tool import grep
from mokioclaw.tools.mcp_tools import search_mcp_tools
from mokioclaw.tools.registry import build_tools

PROJECT = Path(__file__).resolve().parents[1]
MODES = ("full", "light", "auto")


class Reply:
    def invoke(self, messages):
        return AIMessage(content="ROOT_OK DIR_OK 项目规则已加载。")


def runtime_at(root):
    root.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(workspace=root, approval_mode="auto", run_control=RunControl())
    return runtime


def rules(root, real):
    runtime = runtime_at(root)
    (root / "AGENTS.md").write_text("用中文回答，并在回答中包含 ROOT_OK。")
    (root / "src").mkdir()
    (root / "src/AGENTS.md").write_text("讨论本目录代码时，在回答中包含 DIR_OK。")
    (root / "src/app.py").write_text("def add(a, b):\n    return a + b\n")
    read = read_file(runtime, "src/app.py")
    events = []
    try:
        result = invoke_streaming(
            create_model() if real else Reply(),
            [HumanMessage(content="用一句话解释 src/app.py，并遵守项目规则：\n" + read["content"])],
            state={"runtime": runtime},
            writer=events.append,
            node="rules_demo",
        )
        text = str(result.content)
        return {
            "passed": "ROOT_OK" in text and "DIR_OK" in text,
            "reply": text,
            "stream_matches_result": "".join(e["text"] for e in events if e["type"] == "llm_token")
            == text,
            "metrics": runtime.model_metrics,
        }
    finally:
        runtime.run_control.close()


def search(root, real):
    runtime = runtime_at(root)
    (root / "ignored").mkdir()
    (root / ".gitignore").write_text("ignored/\n")
    for i in range(300):
        (root / "ignored" / f"{i}.txt").write_text("needle\n" * 100)
        (root / f"{i:04}.txt").write_text("other text\n" * 20)
    (root / "中文.py").write_text("def needle():\n    pass\n")
    fast = grep(runtime, "needle", head_limit=10)
    runtime.settings = {"search": {"backend": "python"}}
    slow = grep(runtime, "needle", head_limit=10)
    runtime.run_control.close()
    return {
        "passed": fast["matches"] == slow["matches"] and len(fast["matches"]) == 1,
        "backend": fast["backend"],
        "rg_ms": fast["elapsed_ms"],
        "python_ms": slow["elapsed_ms"],
        "matches": fast["matches"],
    }


def inbox(root, real):
    runtime = runtime_at(root)
    queue = RunInbox(root / "messages.json")
    first = queue.submit("demo-message", "不要修改数据库")
    repeated = queue.submit("demo-message", "不要修改数据库")
    state = {
        "runtime": runtime,
        "todos": [{"content": "finished work", "status": "completed"}],
    }
    queue.apply(state)
    runtime.run_control.close()
    return {
        "passed": first["seq"] == repeated["seq"]
        and len(state["latest_user_corrections"]) == 1
        and queue.seal(),
        "corrections": state["latest_user_corrections"],
        "completed_todo_retained": state["todos"][0]["status"] == "completed",
    }


def mcp(root, real):
    runtime = runtime_at(root)
    runtime.mcp_servers = [
        {
            "name": "echo",
            "description": "local echo service",
            "command": sys.executable,
            "args": [str(PROJECT / "examples/mcp/echo_server.py")],
        }
    ]
    for name in ("notes", "metrics"):
        runtime.mcp_servers.append(
            {
                **runtime.mcp_servers[0],
                "name": name,
                "description": f"unrelated {name} service",
            }
        )
    eager = runtime_at(root / "eager")
    eager.mcp_servers = runtime.mcp_servers
    eager.mcp_loading = "eager"

    def schema_bytes(tools):
        return len(
            json.dumps(
                [convert_to_openai_tool(tool) for tool in tools], ensure_ascii=False
            ).encode()
        )

    try:
        native = build_tools(runtime)
        eager_tools = build_tools(eager)
        before = len(runtime.mcp_diagnostics)
        loaded = search_mcp_tools(runtime, server="echo")
        inject_instructions(runtime, [])
        tool = next(t for t in build_tools(runtime) if t.name == loaded["tools"][0]["name"])
        result = execute_tool(runtime, tool, {"text": "hello"}, tool_call_id="demo-echo")
        return {
            "passed": before == 0 and result["ok"],
            "initial_mcp_tools": sum(t.name.startswith("MCP_") for t in native),
            "deferred_initial_schema_bytes": schema_bytes(native),
            "eager_initial_schema_bytes": schema_bytes(eager_tools),
            "deferred_servers_started": sorted({e["server"] for e in runtime.mcp_diagnostics}),
            "eager_servers_started": sorted({e["server"] for e in eager.mcp_diagnostics}),
            "loaded_tools": loaded["tools"],
            "operations": [
                {"phase": e["phase"], "status": e["status"]} for e in runtime.mcp_diagnostics
            ],
            "result": result,
        }
    finally:
        runtime.run_control.close()
        eager.run_control.close()


def hooks(root, real):
    runtime = runtime_at(root)
    runtime.settings = {
        "hooks": {
            "rules": [
                {
                    "event": "PreToolUse",
                    "tool": "FileWriteTool",
                    "args": {"file_path": "^generated/"},
                    "action": "block",
                    "message": "生成目录禁止直接修改",
                }
            ]
        }
    }
    tool = next(t for t in build_tools(runtime) if t.name == "FileWriteTool")
    events = []
    result = execute_tool(
        runtime,
        tool,
        {"file_path": "generated/a.txt", "content": "blocked"},
        on_event=events.append,
    )
    runtime.run_control.close()
    return {
        "passed": not result["ok"] and not (root / "generated/a.txt").exists(),
        "result": result,
        "events": events,
    }


def changes(root, real):
    runtime = runtime_at(root)
    (root / "app.txt").write_bytes(b"before\r\n")
    read_file(runtime, "app.txt")
    result = edit_file(runtime, "app.txt", "before", "after")
    preview = rewind(root, runtime.run_id)
    restored = rewind(root, runtime.run_id, apply=True)
    runtime.run_control.close()
    return {
        "passed": result["ok"]
        and restored["ok"]
        and (root / "app.txt").read_bytes() == b"before\r\n",
        "preview": preview,
        "restored": restored,
    }


def workflows(root, real, *, cross_module=False, modes=None):
    rows = []
    with ExitStack() as stack:
        if not real:
            for name in (
                "mokioclaw.graph.nodes",
                "mokioclaw.agents.code_agent",
                "mokioclaw.agents.repo_agent",
            ):
                stack.enter_context(
                    patch(
                        name + ".create_model",
                        CrossModuleModel if cross_module else WorkflowModel,
                    )
                )
        for mode in modes or MODES:
            workspace = root / mode
            workspace.mkdir(parents=True)
            (workspace / "app.txt").write_text("before")
            if cross_module:
                (workspace / "config.txt").write_text("before")
            started = time.monotonic()
            events = list(
                stream_agent_events(
                    "将 app.txt 和 config.txt 中的 before 均改为 after，这两个文件是相关模块的配置，需要一起修改并分别读取验证。"
                    if cross_module
                    else "把 app.txt 中的 before 改为 after，仅修改这个文件，读取验证结果。",
                    workspace=workspace,
                    workflow_mode=mode,
                    approval_mode="deny",
                    max_attempts=2,
                )
            )
            terminal = next(
                row["event"]
                for row in reversed(events)
                if row.get("event", {}).get("type") == "run_finished"
            )
            changed = [
                item["path"]
                for manifest in (workspace / ".mokioclaw/changes").glob("*/manifest.json")
                for item in json.loads(manifest.read_text())["changes"]
                if item["status"] == "applied"
            ]
            contents = {
                name: (workspace / name).read_text()
                for name in ("app.txt", "config.txt")
                if (workspace / name).exists()
            }
            rows.append(
                {
                    "mode": mode,
                    "passed": terminal["status"] == "finished"
                    and contents["app.txt"].splitlines() == ["after"]
                    and (not cross_module or contents["config.txt"].splitlines() == ["after"]),
                    "selected": [
                        row["event"]["mode"]
                        for row in events
                        if row.get("event", {}).get("type") == "workflow_selected"
                    ],
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                    "metrics": terminal.get("model_metrics", {}),
                    "file_change_records": changed,
                    "actual_contents": contents,
                }
            )
    return {"passed": all(row["passed"] for row in rows), "runs": rows}


def cross_module(root, real):
    return workflows(root, real, cross_module=True)


def steering(root, real):
    runtime = runtime_at(root)
    recorder = TraceRecorder(runtime, task="Create blocked.txt, then accept a correction")
    recorder.start({"runtime": runtime, "task": "steering"})
    sent = False
    observed = []

    class LocalSteering:
        def bind_tools(self, tools):
            return self

        def invoke(self, messages):
            if any("只回复" in str(m.content) for m in messages):
                return AIMessage(content="已收到")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileWriteTool",
                        "id": "old-call",
                        "args": {"file_path": "blocked.txt", "content": "original"},
                    }
                ],
            )

    def step(state):
        nonlocal sent
        raw_writer = get_stream_writer()

        def writer(event):
            nonlocal sent
            observed.append(event)
            recorder.record_emitted_event(event)
            raw_writer(event)
            if event["type"] == "llm_stream_start" and not sent:
                sent = True
                runtime.run_control.inbox.submit(
                    "correction", "只回复已收到，不得创建文件或调用工具。"
                )

        tools = [t for t in build_tools(runtime) if t.name == "FileWriteTool"]
        model = (create_model() if real else LocalSteering()).bind_tools(tools)
        prompt = (
            "只回复已收到，不得创建文件或调用工具。"
            if state.get("latest_user_corrections")
            else "使用 FileWriteTool 创建 blocked.txt，内容为 original。"
        )
        response = invoke_streaming(
            model,
            [
                SystemMessage(content="Follow the latest user request."),
                HumanMessage(content=prompt),
            ],
            writer=writer,
            node="steering_demo",
            state=state,
        )
        for call in response.tool_calls:
            execute_tool(
                runtime,
                tools[0],
                call["args"],
                tool_call_id=call["id"],
                on_event=writer,
            )
        return {"final_answer": str(response.content)}

    graph = StateGraph(MokioGraphState)
    graph.add_node("step", step)
    graph.add_edge(START, "step")
    graph.add_edge("step", END)
    state = {"runtime": runtime, "task": "create file", "messages": []}
    try:
        for envelope in _graph_envelopes(graph.compile(), state, terminal=True):
            if envelope["type"] == "custom_event":
                recorder.record_custom_event(envelope["event"])
        recorder.end(status="finished", latest_node="step", final_state=state)
        return {
            "passed": sent
            and not (root / "blocked.txt").exists()
            and "已收到" in state.get("final_answer", ""),
            "reply": state.get("final_answer"),
            "discarded_calls": sum(
                e["type"] == "tool_result" and e.get("result", {}).get("status") == "not_executed"
                for e in observed
            ),
            "metrics": runtime.model_metrics,
        }
    finally:
        runtime.run_control.close()


SCENARIOS = {
    "rules": rules,
    "search": search,
    "inbox": inbox,
    "mcp": mcp,
    "hooks": hooks,
    "rewind": changes,
    "workflows": workflows,
    "cross_module": cross_module,
    "steering": steering,
}


def main():
    global MODES
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=["all", *SCENARIOS], default="all")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--modes", choices=["full", "light", "auto"], nargs="+", default=list(MODES)
    )
    args = parser.parse_args()
    MODES = tuple(args.modes)
    load_dotenv(PROJECT / ".env")
    root = Path(tempfile.mkdtemp(prefix="mokioclaw-optimization-"))
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "source_ref": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=PROJECT, text=True
        ).strip(),
        "real_model": args.real_model,
        "model": os.getenv("MODEL") if args.real_model else "local deterministic fixture",
        "workspace": str(root),
        "results": {},
    }
    for name, function in SCENARIOS.items():
        if args.scenario not in {"all", name}:
            continue
        started = time.monotonic()
        try:
            result = function(root / name, args.real_model)
        except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}
        result["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        report["results"][name] = result
        print(json.dumps({"scenario": name, **result}, ensure_ascii=False), flush=True)
    report["passed"] = all(result["passed"] for result in report["results"].values())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
