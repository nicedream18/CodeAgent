"""Opt-in real-model acceptance using the configured endpoint.

Run: uv run python examples/context_governance_demo.py --workspace /tmp/mokio-context-demo
The harness seeds history with actual bounded file reads to force automatic
and manual compaction. The continuation uses the production model transport
and tool executor. It does not access the web or use a simulated model.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.context_control import ContextControl
from mokioclaw.core.session import (
    append_assistant_turn,
    append_user_turn,
    build_session_context,
    compact_session_history,
    load_or_create_session,
    save_session,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tool


def run(workspace: Path) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / "report.json").exists():
        raise ValueError("use a fresh workspace; report.json already exists")
    expected = "PASS_" + uuid4().hex[:12]
    source = "https://example.test/context-governance-source"
    text = (
        ("Historical filler line for budget experiment.\n" * 5000) + "\nTAIL_CRITERION: " + expected
    )
    (workspace / "spec.txt").write_text(text, encoding="utf-8")
    control = ContextControl()
    runtime = RuntimeState(
        workspace=workspace,
        approval_mode="auto",
        context_token_limit=24000,
        model_context_reserve_tokens=1024,
        context_safety_tokens=512,
        context_control=control,
    )
    task = (
        f"Create report.json containing exactly the JSON keys criterion and source. "
        f"Read the actual TAIL_CRITERION value from spec.txt with char_offset={len(text) - 100}, char_limit=100. "
        f"Set criterion to that value and source to {source}. Use FileReadTool and FileWriteTool. "
        "Historical filler output is irrelevant. Finish only after writing the file; do not repeat a successful write."
    )
    state = {
        "runtime": runtime,
        "task": task,
        "sources": [{"url": source}],
        "acceptance_criteria": [
            "criterion equals actual spec tail",
            "source equals the given URL",
        ],
    }
    tools = {name: build_tool(runtime, name) for name in ("FileReadTool", "FileWriteTool")}
    model = create_model().bind_tools(list(tools.values()))
    messages = [
        SystemMessage(
            content="You are an implementation agent. Use the supplied tools to complete the task. Keep all current requirements after compaction."
        ),
        HumanMessage(content=task),
    ]
    events = []

    def writer(event):
        if event.get("type") != "llm_token":
            events.append(event)
        if event.get("type") in {"context_compression", "context_compression_failed"}:
            print(
                json.dumps(
                    {
                        key: event.get(key)
                        for key in (
                            "type",
                            "reason",
                            "before_tokens",
                            "after_tokens",
                            "status",
                            "degraded",
                        )
                    }
                ),
                flush=True,
            )

    def seed_history(round_id):
        for index in range(4):
            call_id = f"seed-{round_id}-{index}"
            args = {"file_path": "spec.txt", "offset": index * 20, "limit": 500}
            result = execute_tool(
                runtime,
                tools["FileReadTool"],
                args,
                on_event=writer,
                tool_call_id=call_id,
            )
            messages.append(
                AIMessage(
                    content="",
                    tool_calls=[{"id": call_id, "name": "FileReadTool", "args": args}],
                )
            )
            messages.append(
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False),
                    tool_call_id=call_id,
                    name="FileReadTool",
                )
            )

    seed_history("automatic")
    manual = None
    writes = 0
    for _step in range(6):
        response = invoke_streaming(model, messages, state=state, writer=writer, node="demo")
        messages.append(response)
        calls = getattr(response, "tool_calls", [])
        if not calls:
            break
        for call in calls:
            name = call["name"]
            if name not in tools:
                raise ValueError("model requested an unavailable tool")
            result = execute_tool(
                runtime,
                tools[name],
                call["args"],
                on_event=writer,
                tool_call_id=call["id"],
            )
            messages.append(
                ToolMessage(
                    content=json.dumps(result, ensure_ascii=False),
                    name=name,
                    tool_call_id=call["id"],
                )
            )
            if name == "FileWriteTool" and result.get("ok") and not result.get("already_applied"):
                writes += 1
        if manual is None:
            seed_history("manual")
            manual = control.request()
            print(
                json.dumps({"manual_request": manual["request_id"], "status": manual["status"]}),
                flush=True,
            )
    report = json.loads((workspace / "report.json").read_text(encoding="utf-8"))
    assert report == {"criterion": expected, "source": source}, report
    assert writes == 1, "a completed write was repeated"
    automatic = [
        event
        for event in events
        if event.get("type") == "context_compression" and event.get("reason") == "automatic"
    ]
    assert automatic, "automatic compaction did not occur"
    assert manual and control.get(manual["request_id"])["status"] == "applied"
    budgets = [event for event in events if event.get("type") == "context_budget"]
    assert all(event["input_tokens"] <= event["token_limit"] for event in budgets)

    session = load_or_create_session(workspace)
    turn = append_user_turn(session, task)
    for _ in range(8):
        append_assistant_turn(
            session,
            turn=turn,
            route="workflow",
            content="Historical observations. " * 80,
        )
    save_session(workspace, session)
    saved_compact = compact_session_history(workspace, keep_recent=2)
    followup = [
        SystemMessage(content="Answer using the saved session requirements."),
        HumanMessage(
            content=build_session_context(workspace)
            + "\nReturn only the source URL required in the previous task."
        ),
    ]
    answer = invoke_streaming(
        create_model(), followup, state=state, writer=writer, node="demo-followup"
    )
    assert source in str(answer.content), "session compact lost the required source URL"
    result = {
        "status": "passed",
        "model": os.getenv("MODEL"),
        "workspace": str(workspace),
        "automatic_compactions": len(automatic),
        "manual_run": control.get(manual["request_id"]),
        "manual_session": saved_compact,
        "artifact_verified": True,
        "successful_writes": writes,
        "request_count": len(budgets),
        "events": events,
    }
    (workspace / "acceptance.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def run_workflow(workspace: Path) -> dict:
    """A real router → planner → codeAgent → verifier acceptance run."""
    from mokioclaw.core.agent import stream_agent_events

    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / "report.json").exists():
        raise ValueError("use a fresh workspace; report.json already exists")
    expected = "PASS_" + uuid4().hex[:12]
    source = "https://example.test/context-workflow-source"
    spec = ("x" * 300 + "\n") * 1500 + "\nTAIL_CRITERION: " + expected
    (workspace / "spec.txt").write_text(spec)
    os.environ.update(
        MOKIO_CONTEXT_TOKEN_LIMIT="48000",
        MOKIO_MODEL_CONTEXT_RESERVE_TOKENS="2048",
        MOKIO_CONTEXT_SAFETY_TOKENS="1024",
        MOKIO_CONTEXT_HIGH_RATIO="0.75",
        MOKIO_CONTEXT_TARGET_RATIO="0.45",
        MOKIO_USE_DAEMON="0",
        MOKIO_SUBAGENTS='{"codeAgent":{"max_tool_calls":20}}',
    )
    task = (
        "在当前工作区完成本地文件任务，不需要联网。用 FileReadTool 分别读取 spec.txt 的三个行范围："
        "offset=0,100,200，每次 limit=100，返回的有界样本足够，不必补读填充段。"
        f"然后用 char_offset={len(spec) - 100}, char_limit=100 读取文件末尾，找到 TAIL_CRITERION 的真实值。"
        f"创建 report.json，只包含 criterion 和 source 两个键，criterion 为文件末尾的值，source 为 {source}。"
        "最后用 Python 实际核验 JSON 与 spec.txt 的末尾标记一致。不要修改 spec.txt。完成后简短说明结果。"
    )
    events, verified = [], False
    for envelope in stream_agent_events(
        task,
        workspace=workspace,
        max_attempts=2,
        approval_mode="auto",
        checkpoint_mode="light",
        trace_mode="on",
        use_daemon=False,
    ):
        if envelope.get("type") == "custom_event":
            event = envelope.get("event", {})
            if event.get("type") != "llm_token":
                events.append(event)
            if event.get("type") in {
                "context_compression",
                "context_compression_failed",
            }:
                print(
                    json.dumps(
                        {
                            key: event.get(key)
                            for key in (
                                "type",
                                "agent",
                                "reason",
                                "before_tokens",
                                "after_tokens",
                                "status",
                            )
                        }
                    ),
                    flush=True,
                )
        if envelope.get("type") == "graph_event":
            for update in envelope.get("event", {}).values():
                if isinstance(update, dict) and "passed" in update:
                    verified = bool(update["passed"])
    report = json.loads((workspace / "report.json").read_text())
    assert report == {"criterion": expected, "source": source}
    assert (workspace / "spec.txt").read_text() == spec
    assert verified, "the workflow verifier did not pass"
    budgets = [event for event in events if event.get("type") == "context_budget"]
    assert all(event["input_tokens"] <= event["token_limit"] for event in budgets)
    compactions = [event for event in events if event.get("type") == "context_compression"]
    assert compactions, "full workflow did not trigger compaction"
    actual_writes = sum(
        event.get("type") == "tool_result"
        and event.get("name") == "FileWriteTool"
        and isinstance(event.get("result"), dict)
        and event["result"].get("ok")
        and not event["result"].get("already_applied")
        for event in events
    )
    assert actual_writes == 1, "completed report write was executed again"
    result = {
        "status": "passed",
        "mode": "full-workflow",
        "model": os.getenv("MODEL"),
        "artifact_verified": True,
        "workflow_verifier_passed": verified,
        "compactions": compactions,
        "report": report,
        "request_count": len(budgets),
        "successful_writes": actual_writes,
        "events": events,
    }
    (workspace / "acceptance.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--full-workflow",
        action="store_true",
        help="Exercise the real supervisor graph instead of seeded history.",
    )
    args = parser.parse_args()
    try:
        result = (run_workflow if args.full_workflow else run)(args.workspace.resolve())
        summary = {
            key: result[key]
            for key in (
                "status",
                "model",
                "artifact_verified",
                "successful_writes",
                "request_count",
            )
        }
        summary["compactions"] = (
            len(result.get("compactions", []))
            if args.full_workflow
            else result["automatic_compactions"]
        )
        print(json.dumps(summary), flush=True)
    except Exception as exc:  # noqa: BLE001 - acceptance report records any terminal failure
        # Never expose a credential if a compatible endpoint echoes it in errors.
        error = str(exc).replace(os.getenv("API_KEY", "<missing>"), "[redacted]")[:500]
        failure = {"status": "failed", "error_type": type(exc).__name__, "error": error}
        args.workspace.mkdir(parents=True, exist_ok=True)
        (args.workspace / "acceptance-failed.json").write_text(
            json.dumps(failure, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(failure, ensure_ascii=False), flush=True)
        raise SystemExit(1) from None
