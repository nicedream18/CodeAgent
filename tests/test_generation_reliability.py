from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage

from mokioclaw.core.run_control import RunControl, RunInterrupted
from mokioclaw.core.state import RuntimeState
from mokioclaw.providers.generation import ModelGenerationStopped
from mokioclaw.providers.openai_provider import invoke_streaming


class Model:
    def __init__(self, replies):
        self.replies, self.requests = replies, []

    def stream(self, messages):
        self.requests.append(list(messages))
        yield self.replies[len(self.requests) - 1]


def cut():
    return AIMessageChunk(
        content="part",
        tool_call_chunks=[
            {
                "name": "FileWriteTool",
                "args": '{"file_path":"a","content":"complete"}',
                "id": "a",
                "index": 0,
            },
            {
                "name": "FileWriteTool",
                "args": '{"file_path":"b","content":"part',
                "id": "b",
                "index": 1,
            },
        ],
        response_metadata={"finish_reason": "length"},
    )


def test_truncated_batch_is_closed_and_replanned_once(tmp_path):
    model = Model([cut(), AIMessage(content="complete")])
    events = []
    runtime = RuntimeState(workspace=tmp_path)
    result = invoke_streaming(
        model, [HumanMessage(content="task")], writer=events.append, state={"runtime": runtime}
    )
    assert result.content == "complete" and len(model.requests) == 2
    assert len([m for m in model.requests[1] if isinstance(m, ToolMessage)]) == 2
    assert (
        len(
            [
                e
                for e in events
                if e["type"] == "tool_result" and e["result"]["status"] == "not_executed"
            ]
        )
        == 2
    )
    ends = [e for e in events if e["type"] == "llm_stream_end"]
    assert [e["status"] for e in ends] == ["failed", "finished"]
    assert len({e["stream_id"] for e in ends}) == 2
    assert runtime.model_metrics["requests"] == 2
    assert not (tmp_path / "a").exists()


def test_second_truncation_stops_without_third_request():
    model = Model([cut(), cut()])
    with pytest.raises(ModelGenerationStopped, match="model_output_truncated"):
        invoke_streaming(model, [])
    assert len(model.requests) == 2


@pytest.mark.parametrize("reason", ["stop", None])
def test_partial_json_never_uses_sdk_tolerant_parse(reason):
    message = cut()
    message.response_metadata = {} if reason is None else {"finish_reason": reason}
    assert message.tool_calls  # SDK has already repaired the invalid JSON
    with pytest.raises(ModelGenerationStopped, match="model_tool_arguments_invalid"):
        invoke_streaming(Model([message]), [])


@pytest.mark.parametrize("reason", ["content_filter", "unexpected"])
def test_abnormal_finish_does_not_retry(reason):
    model = Model([AIMessage(content="blocked", response_metadata={"finish_reason": reason})])
    with pytest.raises(ModelGenerationStopped):
        invoke_streaming(model, [])
    assert len(model.requests) == 1


def test_cancel_prevents_regeneration(tmp_path):
    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)
    model = Model([cut()])

    def writer(event):
        if event["type"] == "llm_stream_end":
            control.cancel()

    try:
        with pytest.raises(RunInterrupted):
            invoke_streaming(model, [], state={"runtime": runtime}, writer=writer)
        assert len(model.requests) == 1
    finally:
        control.close()


def test_correction_prevents_retry(tmp_path):
    from mokioclaw.core.run_inbox import RunRedirected

    control = RunControl()
    runtime = RuntimeState(workspace=tmp_path, run_control=control)
    model = Model([cut()])

    def writer(event):
        if event["type"] == "llm_stream_end":
            control.inbox.submit("new", "Do not write files")

    try:
        with pytest.raises(RunRedirected):
            invoke_streaming(model, [], state={"runtime": runtime}, writer=writer)
        assert len(model.requests) == 1
    finally:
        control.close()


def test_retry_checks_token_budget(tmp_path):
    from mokioclaw.providers.model_policy import RunBudgetExceeded

    runtime = RuntimeState(workspace=tmp_path)
    model = Model([cut()])
    with pytest.raises(RunBudgetExceeded):
        invoke_streaming(
            model,
            [],
            state={"runtime": runtime},
            writer=lambda event: (
                setattr(runtime, "run_token_limit", 1)
                if event["type"] == "llm_stream_end"
                else None
            ),
        )
    assert len(model.requests) == 1


@pytest.mark.parametrize("node_name", ["intent_router_node", "chat_responder_node"])
def test_domain_failure_not_downgraded_by_entry_nodes(node_name, tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    monkeypatch.setattr(nodes, "create_model", lambda: Model([cut(), cut()]))
    with pytest.raises(ModelGenerationStopped, match="model_output_truncated"):
        getattr(nodes, node_name)({"task": "test", "runtime": RuntimeState(workspace=tmp_path)})


def test_second_truncation_saves_failed_checkpoint_and_partial_trace(tmp_path, monkeypatch):
    import json

    from mokioclaw.core.agent import stream_agent_events
    from mokioclaw.graph import nodes

    monkeypatch.setattr(nodes, "create_model", lambda: Model([cut(), cut()]))
    events = list(stream_agent_events("test", workspace=tmp_path, checkpoint_mode="strict"))
    terminal = next(e["event"] for e in events if e.get("event", {}).get("type") == "run_finished")
    assert terminal["status"] == "failed" and terminal["stop_reason"] == "model_output_truncated"
    summaries = list((tmp_path / ".mokioclaw/traces").glob("*/summary.json"))
    assert len(summaries) == 1 and json.loads(summaries[0].read_text())["status"] == "failed"
    assert any(
        "part" in p.read_text()
        for p in (tmp_path / ".mokioclaw/diagnostics").glob("model-stream-*")
    )
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()


def test_nested_generation_failure_crosses_tool_executor(tmp_path, monkeypatch):
    from langchain_core.tools import StructuredTool

    from mokioclaw.agents import code_agent
    from mokioclaw.agents.registry import registered_subagents, run_subagent
    from mokioclaw.tools.execution import execute_tool

    model = Model([cut(), cut()])
    model.bind_tools = lambda tools: model
    runtime = RuntimeState(workspace=tmp_path, subagent_registry=registered_subagents())
    monkeypatch.setattr(code_agent, "create_model", lambda: model)
    state = {"runtime": runtime, "task": "test"}

    def runner(instruction: str):
        return run_subagent(state, "codeAgent", instruction)

    tool = StructuredTool.from_function(runner, name="CallCodeAgentTool", description="test")
    with pytest.raises(ModelGenerationStopped, match="model_output_truncated"):
        execute_tool(runtime, tool, {"instruction": "edit"}, tool_call_id="outer")
    assert len(model.requests) == 2
    assert not (tmp_path / "a").exists()


@pytest.mark.parametrize(
    "chunks", [[], [object()], [AIMessage(content="a"), AIMessage(content="b")]]
)
def test_unparseable_stream_is_controlled_and_not_retried(chunks):
    class Invalid:
        requests = 0

        def stream(self, messages):
            self.requests += 1
            yield from chunks

    model = Invalid()
    with pytest.raises(ModelGenerationStopped):
        invoke_streaming(model, [])
    assert model.requests == 1
