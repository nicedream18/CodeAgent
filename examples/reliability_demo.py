"""Fixed offline/real-model reliability benchmarks. Writes observations, never credentials."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from dotenv import load_dotenv
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from optimization_demo import steering, workflows

from mokioclaw.agents.code_agent import run_code_agent
from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.approval_rules import runtime_rules
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.state import RuntimeState
from mokioclaw.providers.generation import ModelGenerationStopped
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.mcp_tools import build_mcp_tools
from mokioclaw.tools.registry import build_tool

PROJECT = Path(__file__).resolve().parents[1]
MODES = ("full", "light", "auto")


class Fixed:
    def __init__(self, replies):
        self.replies, self.index = replies, 0

    def bind_tools(self, tools):
        return self

    def stream(self, messages):
        message = self.replies[self.index]
        self.index += 1
        yield message


def truncation(root, real):
    runtime = RuntimeState(workspace=root, run_control=RunControl())
    events = []
    if real:
        model = create_model()
        model.mokio_profile = {"context_tokens": 32768, "output_tokens": 64}
        prompt = "Write a JSON object with 2000 numbered entries explaining Python concurrency. Continue until every entry is complete."
    else:
        model = Fixed(
            [
                AIMessageChunk(
                    content="partial",
                    tool_call_chunks=[
                        {
                            "name": "FileWriteTool",
                            "args": '{"file_path":"bad","content":"incomplete',
                            "id": "bad",
                            "index": 0,
                        }
                    ],
                    response_metadata={"finish_reason": "length"},
                ),
                AIMessage(
                    content="Complete concise response", response_metadata={"finish_reason": "stop"}
                ),
            ]
        )
        prompt = "Create a file"
    stopped = None
    try:
        try:
            result = invoke_streaming(
                model,
                [HumanMessage(content=prompt)],
                state={"runtime": runtime},
                writer=events.append,
                node="truncate_demo",
            )
            answer = str(result.content)
        except ModelGenerationStopped as exc:
            stopped = exc.reason
            answer = ""
        ends = [e for e in events if e["type"] == "llm_stream_end"]
        return {
            "passed": len(ends) == 2
            and ends[0]["raw_finish_reason"] == "length"
            and (ends[-1]["status"] == "finished" or stopped == "model_output_truncated")
            and not (root / "bad").exists(),
            "stream_ends": ends,
            "stop_reason": stopped,
            "answer": answer,
            "metrics": runtime.model_metrics,
        }
    finally:
        runtime.run_control.close()


def explicit_skill(root, real):
    runtime = RuntimeState(
        workspace=root,
        explicit_skill="review",
        run_control=RunControl(),
        active_agent_tools={"FileReadTool"},
    )
    (root / "app.txt").write_text("before")
    path = root / ".mokioclaw/skills/review/SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: review\ndescription: Read-only reviewer\ntools: [FileReadTool]\npermissions: [filesystem_read]\n---\nRead app.txt, then respond with SKILL_MARKER and its exact content. Do not write any files.\n"
    )
    replies = [
        AIMessage(
            content="",
            tool_calls=[{"id": "r", "name": "FileReadTool", "args": {"file_path": "app.txt"}}],
        ),
        AIMessage(content="SKILL_MARKER before"),
    ]
    with ExitStack() as stack:
        if not real:
            stack.enter_context(
                patch("mokioclaw.agents.code_agent.create_model", lambda: Fixed(replies))
            )
        try:
            result = run_code_agent(
                {"runtime": runtime, "task": "Review app.txt"}, "Use required Skill", max_loops=4
            )
            return {
                "passed": "SKILL_MARKER" in result["summary"]
                and "before" in result["summary"]
                and (root / "app.txt").read_text() == "before",
                "answer": result["summary"],
                "metrics": runtime.model_metrics,
            }
        finally:
            runtime.run_control.close()


def approval(root, real):
    runtime = RuntimeState(
        workspace=root,
        session_id="benchmark-session",
        run_control=RunControl(),
        tool_policies={"FileReadTool": "ask"},
    )
    approvals = []
    runtime.approval_handler = lambda request: (
        approvals.append(request.id) or ApprovalDecision(True, scope="session")
    )
    (root / "app.txt").write_text("MOKIO_APPROVAL")
    tool = build_tool(runtime, "FileReadTool")
    args = {"file_path": "app.txt"}
    try:
        if real:
            response = invoke_streaming(
                create_model().bind_tools([tool]),
                [
                    HumanMessage(
                        content='Call FileReadTool once with file_path exactly "app.txt" and no other arguments.'
                    )
                ],
                state={"runtime": runtime},
                node="approval_demo",
            )
            if not response.tool_calls:
                return {
                    "passed": False,
                    "error": "model did not call requested tool",
                    "metrics": runtime.model_metrics,
                }
            args = response.tool_calls[0]["args"]
            if args.get("file_path") != "app.txt":
                return {
                    "passed": False,
                    "error": "model changed fixed benchmark file",
                    "metrics": runtime.model_metrics,
                }
        first = execute_tool(runtime, tool, args, tool_call_id="first")
        second = execute_tool(runtime, tool, args, tool_call_id="second")
        manager = runtime_rules(runtime)
        rules = manager.list_rules()["rules"]
        manager.revoke(clear=True)
        return {
            "passed": first["ok"] and second["ok"] and len(approvals) == 1,
            "approval_count": len(approvals),
            "rules": rules,
            "metrics": runtime.model_metrics,
        }
    finally:
        runtime.run_control.close()


def mcp_fault(root, real):
    runtime = RuntimeState(
        workspace=root, approval_mode="auto", run_control=RunControl(), mcp_timeout_seconds=2
    )
    script = root / "bad_server.py"
    script.write_text('import sys\nsys.stderr.write("intentional failure\\n")\nsys.exit(7)\n')
    runtime.mcp_servers = [
        {"name": "bad", "command": sys.executable, "args": [str(script)]},
        {
            "name": "echo",
            "command": sys.executable,
            "args": [str(PROJECT / "examples/mcp/echo_server.py")],
        },
    ]
    try:
        tools = build_mcp_tools(runtime)
        selected = next(t for t in tools if t.name == "MCP_echo_echo")
        result = execute_tool(
            runtime, selected, {"text": "still works"}, tool_call_id="after-fault"
        )
        return {
            "passed": bool(runtime.mcp_discovery_errors) and result["ok"],
            "errors": runtime.mcp_discovery_errors,
            "diagnostics": runtime.mcp_diagnostics,
            "metrics": runtime.model_metrics,
        }
    finally:
        runtime.run_control.close()


def sample(root, scenario, mode, repeat, real):
    root.mkdir(parents=True)
    started = time.monotonic()
    try:
        if scenario in {"single_file", "cross_module"}:
            # The process-wide mode selector is not mutated; workflow runner accepts a tuple below.
            result = workflows(root, real, cross_module=scenario == "cross_module", modes=(mode,))
            result = result["runs"][0]
        else:
            result = {
                "explicit_skill": explicit_skill,
                "truncation": truncation,
                "approval": approval,
                "mcp_fault": mcp_fault,
                "correction": steering,
            }[scenario](root, real)
    except Exception as exc:
        result = {"passed": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}
    return {
        "scenario": scenario,
        "mode": mode,
        "repeat": repeat,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        **result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--modes", nargs="+", choices=MODES, default=MODES)
    parser.add_argument(
        "--scenarios",
        nargs="+",
        choices=[
            "single_file",
            "cross_module",
            "explicit_skill",
            "truncation",
            "approval",
            "mcp_fault",
            "correction",
        ],
        default=[
            "single_file",
            "cross_module",
            "explicit_skill",
            "truncation",
            "approval",
            "mcp_fault",
            "correction",
        ],
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    load_dotenv(PROJECT / ".env")
    if not args.real_model and args.workers != 1:
        parser.error("offline fixtures patch model factories; use one worker")
    root = Path(tempfile.mkdtemp(prefix="mokioclaw-reliability-"))
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, cwd=PROJECT
        ).strip(),
        "source_fingerprints": {
            str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for folder in ("src", "examples")
            for p in (PROJECT / folder).rglob("*.py")
        },
        "lock_sha256": hashlib.sha256((PROJECT / "uv.lock").read_bytes()).hexdigest(),
        "real_model": args.real_model,
        "model": os.getenv("MODEL") if args.real_model else "fixed offline fixture",
        "workspace": str(root),
        "runs": [],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save(row):
        report["runs"].append(row)
        report["passed"] = all(r["passed"] for r in report["runs"])
        report["summary"] = []
        for name in args.scenarios:
            for mode in args.modes:
                rows = [r for r in report["runs"] if r["scenario"] == name and r["mode"] == mode]
                if not rows:
                    continue
                times = sorted(r["duration_ms"] for r in rows)
                requests = [r.get("metrics", {}).get("requests", 0) for r in rows]
                report["summary"].append(
                    {
                        "scenario": name,
                        "mode": mode,
                        "n": len(rows),
                        "success_rate": sum(r["passed"] for r in rows) / len(rows),
                        "duration_ms": {
                            "min": min(times),
                            "median": statistics.median(times),
                            "max": max(times),
                        },
                        "requests": {
                            "min": min(requests),
                            "median": statistics.median(requests),
                            "max": max(requests),
                        },
                    }
                )
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(
            json.dumps(
                {k: row[k] for k in ("scenario", "mode", "repeat", "passed", "duration_ms")},
                ensure_ascii=False,
            ),
            flush=True,
        )

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                sample, root / f"{name}-{mode}-{repeat}", name, mode, repeat, args.real_model
            )
            for name in args.scenarios
            for mode in args.modes
            for repeat in range(1, args.repeats + 1)
        ]
        for future in as_completed(futures):
            save(future.result())
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
