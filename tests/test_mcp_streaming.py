from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from io import StringIO
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.tools import StructuredTool
from rich.console import Console

from mokioclaw.core.approval import ApprovalDecision
from mokioclaw.core.run_control import RunControl, RunInterrupted
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.mcp_tools import build_mcp_tools, emit_mcp_diagnostics


def runtime_at(path, **kwargs):
    path.mkdir(parents=True, exist_ok=True)
    return RuntimeState(workspace=path, approval_mode="auto", **kwargs)


class AsyncModel:
    def __init__(self, chunks=None, delay=0, fail=None):
        self.chunks = (
            chunks
            if chunks is not None
            else [AIMessageChunk(content="中文"), AIMessageChunk(content="输出")]
        )
        self.delay = delay
        self.fail = fail
        self.waiting = threading.Event()
        self.closed = threading.Event()

    async def astream(self, _messages):
        try:
            for chunk in self.chunks:
                self.waiting.set()
                await asyncio.sleep(self.delay)
                yield chunk
            if self.fail:
                raise self.fail
        finally:
            self.closed.set()


@pytest.mark.parametrize("after_first", [False, True])
def test_cancel_async_model_before_first_token_or_between_chunks(tmp_path, after_first):
    control = RunControl()
    runtime = runtime_at(tmp_path, run_control=control)
    model = AsyncModel(delay=10)
    events, errors = [], []
    if after_first:

        async def stream(_messages):
            try:
                yield AIMessageChunk(content="保留这部分")
                model.waiting.set()
                await asyncio.sleep(30)
            finally:
                model.closed.set()

        model.astream = stream

    def consume():
        try:
            invoke_streaming(
                model, [], state={"runtime": runtime}, writer=events.append, node="demo"
            )
        except BaseException as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert model.waiting.wait(2)
        started = time.monotonic()
        control.cancel()
        worker.join(2)
        assert not worker.is_alive()
        assert time.monotonic() - started < 2
        assert isinstance(errors[0], RunInterrupted)
        assert model.closed.is_set()
        assert events[-1]["status"] == "interrupted"
        assert len([event for event in events if event["type"] == "llm_stream_end"]) == 1
        assert len({event["stream_id"] for event in events if "stream_id" in event}) == 1
    finally:
        control.cancel()
        worker.join(3)
        control.close()
    assert not control._thread.is_alive()


def test_async_stream_aggregates_content_tools_and_usage(tmp_path):
    control = RunControl()
    runtime = runtime_at(tmp_path, run_control=control)
    model = AsyncModel(
        chunks=[
            AIMessageChunk(
                content=[{"type": "text", "text": "你好"}],
                tool_call_chunks=[{"name": "echo", "args": '{"text":', "id": "t1", "index": 0}],
            ),
            AIMessageChunk(
                content=[{"type": "text", "text": "世界"}],
                tool_call_chunks=[{"name": None, "args": '"hello"}', "id": None, "index": 0}],
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                },
            ),
        ]
    )
    events = []
    try:
        message = invoke_streaming(model, [], state={"runtime": runtime}, writer=events.append)
        text = "".join(item["text"] for item in events if item["type"] == "llm_token")
        assert text == "你好世界"
        assert message.tool_calls[0]["args"] == {"text": "hello"}
        assert message.usage_metadata["total_tokens"] == 7
        assert (
            next(event for event in events if event["type"] == "llm_stream_end")["status"]
            == "finished"
        )
        # Reuse the same event loop for a second invocation.
        assert (
            invoke_streaming(AsyncModel(), [], state={"runtime": runtime}).content
            == AIMessage(content="中文输出").content
        )
    finally:
        control.close()


@pytest.mark.parametrize(
    "chunks,failure",
    [
        ([], None),
        ([object()], None),
        ([object(), object()], None),
        ([AIMessageChunk(content="部分")], ValueError("broken stream")),
    ],
)
def test_invalid_or_failed_stream_has_one_failed_end(chunks, failure):
    events = []
    with pytest.raises((RuntimeError, ValueError)):
        invoke_streaming(AsyncModel(chunks=chunks, fail=failure), [], writer=events.append)
    assert events[-1]["status"] == "failed"
    assert sum(item["type"] == "llm_stream_end" for item in events) == 1


def test_cancellation_does_not_execute_a_tool_or_enter_fallback(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    runtime = runtime_at(tmp_path, run_control=RunControl())
    runtime.run_control.cancel()
    tool = StructuredTool.from_function(
        name="FileWriteTool", func=lambda: pytest.fail("tool ran"), description="test"
    )
    with pytest.raises(RunInterrupted):
        execute_tool(runtime, tool, {})
    monkeypatch.setattr(nodes, "create_model", lambda: AsyncModel())
    from mokioclaw.agents.search_agent import _execute_search_tool

    with pytest.raises(RunInterrupted):
        _execute_search_tool(
            {"name": "WebSearchTool", "args": {"query": "hello"}},
            state={"runtime": runtime},
        )
    for node in [nodes.intent_router_node, nodes.chat_responder_node]:
        with pytest.raises(RunInterrupted):
            node({"runtime": runtime, "task": "hello"})


def test_trace_separates_simultaneous_streams_and_flushes_interruption(tmp_path):
    trace = TraceRecorder(runtime_at(tmp_path, trace_mode="on"), task="test")
    for stream_id, text in [("one", "甲"), ("two", "乙" * 5000)]:
        trace.record_custom_event(
            {"type": "llm_stream_start", "node": "same", "stream_id": stream_id}
        )
        trace.record_custom_event(
            {"type": "llm_token", "node": "same", "stream_id": stream_id, "text": text}
        )
    trace.end(status="interrupted")
    trace.end(status="interrupted")
    rows = [json.loads(line) for line in (trace.root / "events.jsonl").read_text().splitlines()]
    ends = [row["payload"]["payload"] for row in rows if row["type"] == "custom:llm_stream_end"]
    assert len(ends) == 2
    assert [event["status"] for event in ends] == ["interrupted", "interrupted"]
    assert [(tmp_path / event["full_output_path"]).read_text() for event in ends] == [
        "甲",
        "乙" * 5000,
    ]
    assert sum(row["type"] == "run_end" for row in rows) == 1
    assert not trace.stream_buffers


def test_rich_stream_prints_model_markup_as_literal_text(monkeypatch):
    from mokioclaw.cli import formatter

    output = StringIO()
    monkeypatch.setattr(formatter, "console", Console(file=output, color_system=None, width=200))
    formatter.print_custom_event({"type": "llm_stream_start", "node": "demo", "stream_id": "s"})
    formatter.print_custom_event({"type": "llm_token", "text": "[bold]中文[/bold]"})
    formatter.print_custom_event(
        {
            "type": "llm_stream_end",
            "node": "demo",
            "stream_id": "s",
            "status": "interrupted",
        }
    )
    assert "[bold]中文[/bold]" in output.getvalue()
    assert "interrupted" in output.getvalue()
    assert not formatter._active_llm_streams


@pytest.fixture
def mcp_runtime(tmp_path):
    project = Path(__file__).resolve().parents[1]
    source = (project / "examples/mcp/echo_server.py").read_text()
    source = source.replace(
        "import anyio",
        """import anyio
import os
import sys
from pathlib import Path
with Path(os.environ["TEST_PID_LOG"]).open("a") as handle:
    handle.write(str(os.getpid()) + "\\n")
if os.environ.get("TEST_DISCOVERY") == "crash":
    os._exit(19)
if os.environ.get("TEST_DISCOVERY") == "protocol":
    print("broken protocol", flush=True)
    import time
    time.sleep(20)
""",
    )
    source = source.replace(
        "    del context\n",
        """    del context
    mode = params.arguments.get("text")
    if mode == "crash":
        print("crash detail", file=sys.stderr, flush=True)
        os._exit(17)
    if mode == "slow":
        await anyio.sleep(20)
    if mode == "stderr":
        sys.stderr.write("x" * (2 * 1024 * 1024) + "TAIL_MARK")
        sys.stderr.flush()
    if mode == "error":
        return types.CallToolResult(content=[types.TextContent(type="text", text="tool rejected")], isError=True)
""",
    )
    server = tmp_path / "server.py"
    server.write_text(source)
    runtime = runtime_at(tmp_path / "workspace", run_control=RunControl())
    runtime.mcp_timeout_seconds = 1.5
    runtime.mcp_servers = [
        {
            "name": "test",
            "command": sys.executable,
            "args": [str(server)],
            "env": {"TEST_PID_LOG": str(tmp_path / "pids")},
        }
    ]
    yield runtime
    runtime.run_control.close()
    for pid in (
        map(int, (tmp_path / "pids").read_text().splitlines())
        if (tmp_path / "pids").exists()
        else []
    ):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("hello", True),
        ("crash", False),
        ("slow", False),
        ("stderr", True),
        ("error", False),
    ],
)
def test_mcp_faults_are_bounded_and_next_call_succeeds(mcp_runtime, mode, expected):
    runtime = mcp_runtime
    tool = build_mcp_tools(runtime)[0]
    events = []
    emit_mcp_diagnostics(runtime, events.append)
    result = execute_tool(
        runtime, tool, {"text": mode}, on_event=events.append, tool_call_id="call-test"
    )
    assert result["ok"] is expected
    event = next(
        item for item in events if item["type"] == "mcp_diagnostic" and item["phase"] == "call"
    )
    assert event["tool_call_id"] == "call-test"
    assert event["status"] == ("finished" if expected else "failed")
    if mode == "slow":
        assert result["error_type"] == "transient"
        assert result["retries"] == 0
    if mode == "stderr":
        assert event["output_truncated"]
        assert len(event["stderr"].encode()) <= 4096
        assert event["stderr"].endswith("TAIL_MARK")
        assert (runtime.workspace / event["full_output_path"]).stat().st_size == 1024 * 1024
    assert execute_tool(runtime, tool, {"text": "after"})["ok"] is True


@pytest.mark.parametrize("failure", ["missing", "crash", "protocol"])
def test_mcp_discovery_failure_isolated_from_other_servers(mcp_runtime, failure):
    runtime = mcp_runtime
    broken = {**runtime.mcp_servers[0], "name": "broken"}
    broken["env"] = {**broken["env"], "TEST_DISCOVERY": failure}
    if failure == "missing":
        broken["command"] = "/no-such-mcp-command"
    runtime.mcp_servers.insert(0, broken)
    assert [tool.name for tool in build_mcp_tools(runtime)] == ["MCP_test_echo"]
    assert runtime.mcp_discovery_errors
    assert any(event["status"] == "failed" for event in runtime.mcp_diagnostics)


def test_mcp_approval_denial_does_not_spawn_call_process(mcp_runtime):
    runtime = mcp_runtime
    tool = build_mcp_tools(runtime)[0]
    pid_log = Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"])
    before = pid_log.read_text()
    runtime.approval_mode = "inline"
    runtime.approval_handler = lambda _: ApprovalDecision(False)
    result = execute_tool(runtime, tool, {"text": "hello"})
    assert result["error_type"] == "permission_denied"
    assert pid_log.read_text() == before


def test_mcp_cancel_closes_process_and_reports_interrupted(mcp_runtime):
    runtime = mcp_runtime
    tool = build_mcp_tools(runtime)[0]
    runtime.mcp_timeout_seconds = 30
    events, errors = [], []

    def consume():
        try:
            execute_tool(runtime, tool, {"text": "slow"}, on_event=events.append)
        except RunInterrupted as exc:
            errors.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    pid_log = Path(runtime.mcp_servers[0]["env"]["TEST_PID_LOG"])
    deadline = time.monotonic() + 2
    while len(pid_log.read_text().splitlines()) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    runtime.run_control.cancel()
    worker.join(2)
    assert not worker.is_alive() and errors
    assert events[-1]["status"] == "interrupted"


def test_diagnostic_retention_and_utf8_boundary(tmp_path):
    from mokioclaw.core.artifacts import MAX_TOTAL_BYTES, DiagnosticArtifact

    for _ in range(35):
        artifact = DiagnosticArtifact(tmp_path, "test")
        artifact.write(("中" * 400000).encode())
        metadata = artifact.finish()
        assert metadata["output_truncated"]
        (tmp_path / metadata["full_output_path"]).read_text(encoding="utf-8")
    files = list((tmp_path / ".mokioclaw/diagnostics").glob("*.log"))
    assert len(files) <= 32
    assert sum(path.stat().st_size for path in files) <= MAX_TOTAL_BYTES


def test_sync_subagent_propagates_cancellation_and_restores_scope(tmp_path):
    from mokioclaw.agents.registry import (
        SubagentDefinition,
        SubagentRegistry,
        run_subagent,
    )

    control = RunControl()
    runtime = runtime_at(tmp_path, run_control=control)
    model = AsyncModel(delay=20)

    def runner(input_value, **_kwargs):
        invoke_streaming(model, [], state={"runtime": input_value.runtime})
        pytest.fail("cancelled Subagent continued")

    registry = SubagentRegistry()
    registry.register(
        SubagentDefinition(
            name="demo",
            description="demo",
            legacy_runner=False,
            background_safe=True,
            allowed_tools=frozenset({"FileReadTool"}),
            max_tool_calls=1,
            runner=runner,
        )
    )
    runtime.subagent_registry = registry
    failures = []

    def consume():
        try:
            run_subagent({"runtime": runtime, "task": "demo"}, "demo", "test")
        except RunInterrupted as exc:
            failures.append(exc)

    worker = threading.Thread(target=consume)
    worker.start()
    assert model.waiting.wait(2)
    control.cancel()
    worker.join(2)
    try:
        assert not worker.is_alive() and failures
        assert runtime.active_subagent_budget is None and runtime.active_agent_tools is None
    finally:
        control.close()


def test_mcp_crash_result_does_not_abort_code_agent(mcp_runtime, monkeypatch):
    mcp_runtime.mcp_loading = "eager"
    from mokioclaw.agents import code_agent

    class ScriptedModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, _tools):
            return self

        def bind(self, **_kwargs):
            return self

        def stream(self, _messages):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": "MCP_test_echo",
                            "args": '{"text":"crash"}',
                            "id": "tc1",
                            "index": 0,
                        }
                    ],
                )
            else:
                yield AIMessageChunk(content="服务故障已处理")

    model, events = ScriptedModel(), []
    monkeypatch.setattr(code_agent, "create_model", lambda: model)
    result = code_agent.run_code_agent(
        {"runtime": mcp_runtime, "task": "test"}, "test", writer=events.append
    )
    assert "服务故障已处理" in result["summary"]
    assert any(event["type"] == "tool_result" and not event["result"]["ok"] for event in events)
    assert any(
        event["type"] == "mcp_diagnostic" and event["status"] == "failed" for event in events
    )


def test_bad_or_duplicate_mcp_schema_does_not_abort_registration(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from mokioclaw.tools import mcp_tools

    good = SimpleNamespace(name="echo", description="test", input_schema={"type": "object"})
    bad = SimpleNamespace(name="bad", description="test", input_schema={"properties": []})
    monkeypatch.setattr(
        mcp_tools, "_operation", lambda *_args: SimpleNamespace(tools=[good, bad, good])
    )
    runtime = runtime_at(tmp_path)
    runtime.mcp_servers = [{"name": "demo", "command": "unused"}]
    assert [tool.name for tool in build_mcp_tools(runtime)] == ["MCP_demo_echo"]
    assert len(runtime.mcp_discovery_errors) == 2
