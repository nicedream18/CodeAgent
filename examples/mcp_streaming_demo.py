"""Reproducible streaming/interrupt experiment; --real uses the configured model.

uv run python examples/mcp_streaming_demo.py --workspace /tmp/mokio-stream-demo
uv run python examples/mcp_streaming_demo.py --real --workspace /tmp/mokio-stream-real
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from langchain_core.messages import AIMessageChunk, HumanMessage

from mokioclaw.cli.formatter import print_custom_event
from mokioclaw.core.run_control import RunControl, RunInterrupted
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.providers.openai_provider import create_model, invoke_streaming


class SlowDemoModel:
    async def astream(self, _messages):
        for text in ["你好", "，", "这是", "增量", "输出", "。"]:
            await asyncio.sleep(0.1)
            yield AIMessageChunk(content=text)


def _run_once(workspace: Path, real: bool, cancel: bool) -> dict:
    control = RunControl()
    runtime = RuntimeState(workspace=workspace, run_control=control, trace_mode="on")
    trace = TraceRecorder(runtime, task="streaming acceptance")
    trace.start({"task": "streaming acceptance"})
    events, text_parts = [], []
    started, requested = time.monotonic(), None
    status, aggregate = "finished", None

    def writer(event):
        nonlocal requested
        trace.record_custom_event(event)
        events.append(
            {**event, "elapsed_ms": round((time.monotonic() - started) * 1000)}
        )
        print_custom_event(event)
        if event["type"] == "llm_token":
            text_parts.append(event["text"])
            if cancel and len(text_parts) == 2:
                requested = time.monotonic()
                control.cancel()

    prompt = (
        "请只用一句中文解释生成器。"
        if not cancel
        else "请用中文详细解释生成器，分20节，每节100字。"
    )
    try:
        model = create_model() if real else SlowDemoModel()
        aggregate = invoke_streaming(
            model,
            [HumanMessage(content=prompt)],
            state={"runtime": runtime},
            writer=writer,
            node="demo",
        )
        assert aggregate.content == "".join(text_parts)
    except RunInterrupted:
        status = "interrupted"
    finally:
        control.close()
        trace.end(status=status)
    ends = [item for item in events if item["type"] == "llm_stream_end"]
    assert len(ends) == 1 and ends[0]["status"] == status
    return {
        "status": status,
        "model": getattr(model, "model_name", "local-slow-model"),
        "delta_count": len(text_parts),
        "text": "".join(text_parts),
        "aggregate_matches_deltas": aggregate.content == "".join(text_parts)
        if aggregate is not None
        else None,
        "first_token_ms": next(
            item["elapsed_ms"] for item in events if item["type"] == "llm_token"
        ),
        "cancel_to_stopped_ms": round((time.monotonic() - requested) * 1000)
        if requested is not None
        else None,
        "trace_dir": str(trace.root),
        "stream_id": ends[0]["stream_id"],
    }


def experiment(workspace: Path, real: bool = False) -> dict:
    workspace.mkdir(parents=True, exist_ok=True)
    return {
        "real_model": real,
        "runs": [_run_once(workspace, real, cancel) for cancel in (False, True)],
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = experiment(args.workspace, args.real)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
