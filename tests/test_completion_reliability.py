import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.context_budget import ContextBudget, estimate_messages
from mokioclaw.core.context_compaction import compact_messages
from mokioclaw.core.state import RuntimeState
from mokioclaw.graph.memory import build_layered_memory
from mokioclaw.providers.openai_provider import ConfiguredChatOpenAI
from mokioclaw.providers.token_estimate import compatible_estimate, encoding


def test_compatible_counter_includes_schemas_without_network_or_external_cache(monkeypatch):
    import tiktoken.load

    def no_download(*args, **kwargs):
        raise AssertionError("Estimator must not download data")

    monkeypatch.setattr(tiktoken.load, "read_file", no_download)
    encoding.cache_clear()
    model = ConfiguredChatOpenAI(
        api_key="offline-fixture",
        model="unknown-compatible-model",
        base_url="https://offline.invalid/v1",
    )
    tool = {"type": "function", "function": {"name": "inspect", "parameters": {"type": "object"}}}
    messages = [HumanMessage(content="检查中文代码\n" + "return value + 1\n" * 300)]
    plain, method = estimate_messages(model, messages)
    bound, bound_method = estimate_messages(model.bind(tools=[tool]), messages)
    byte_count, _ = estimate_messages(None, messages)
    assert bound > plain > 0
    assert bound < byte_count / 2
    assert method == bound_method == "cl100k_base_estimate"
    extra, _ = compatible_estimate(messages + [AIMessage(content="observed")], [tool])
    assert extra > bound


def test_compaction_keeps_latest_code_even_when_soft_target_cannot_be_reached(tmp_path):
    class Counter:
        def get_num_tokens_from_messages(self, messages):
            return sum(len(str(m.content)) for m in messages)

    task = "immutable task " * 10
    latest = "prefix\n" + "x" * 500 + "\nTHE_IMPLEMENTATION_JUST_READ\n" + "x" * 500
    messages = [SystemMessage(content="rules"), HumanMessage(content=task)]
    for index, content in enumerate(["old" * 3000, latest]):
        messages += [
            AIMessage(
                content="", tool_calls=[{"name": "FileReadTool", "args": {}, "id": str(index)}]
            ),
            ToolMessage(content=content, tool_call_id=str(index)),
        ]
    runtime = RuntimeState(workspace=tmp_path)
    state = {
        "runtime": runtime,
        "task": task,
        "repo_explored": True,
        "repo_context": "observed map",
    }
    budget = ContextBudget(window=3500, output=200, safety=100, target_ratio=0.3)
    replacement, event = compact_messages(
        Counter(), messages, state, budget=budget, summarizer=lambda *_: {"summary": "short"}
    )
    assert event["status"] == "applied" and not event["target_reached"]
    assert event["after_tokens"] <= budget.input_limit
    assert any(isinstance(m, ToolMessage) and m.content == latest for m in replacement)
    summary = next(
        json.loads(m.content) for m in replacement if isinstance(m, AIMessage) and not m.tool_calls
    )
    assert summary["facts"]["repo_explored"] is True
    assert summary["facts"]["repo_context"] == "observed map"


def test_large_repository_map_is_bounded_in_prompt_and_exact_in_reference(tmp_path):
    runtime = RuntimeState(workspace=tmp_path)
    original = "仓库证据" * 3000
    memory = build_layered_memory({"runtime": runtime, "task": "inspect", "repo_context": original})
    projection = memory["working_memory"]["repo_context"]
    assert len(projection) < 2600
    reference = projection.split("Full repository evidence: ")[1]
    assert json.loads((tmp_path / reference).read_text())["repo_context"] == original


@pytest.mark.parametrize("role", ["repoAgent", "codeAgent"])
def test_tool_budget_returns_one_report_and_closes_unexecuted_batch(tmp_path, monkeypatch, role):
    from mokioclaw.agents import code_agent, repo_agent
    from mokioclaw.agents.registry import run_subagent
    from mokioclaw.core.context_compaction import message_groups

    (tmp_path / "a.py").write_text("observed\n")
    reports = []

    class Bound:
        def invoke(self, messages):
            return AIMessage(
                content="inspect",
                tool_calls=[
                    {"name": "FileReadTool", "args": {"file_path": "a.py"}, "id": f"call-{i}"}
                    for i in range(3)
                ],
            )

    class Model:
        def bind_tools(self, tools):
            return Bound()

        def invoke(self, messages):
            message_groups(messages)
            results = [m for m in messages if isinstance(m, ToolMessage)]
            assert len(results) == 3
            assert sum(json.loads(m.content).get("ok", False) for m in results) == 1
            reports.append(messages)
            return AIMessage(content='{"summary":"a.py observed; no implementation done"}')

    monkeypatch.setattr(code_agent if role == "codeAgent" else repo_agent, "create_model", Model)
    runtime = RuntimeState(workspace=tmp_path, subagent_configs={role: {"max_tool_calls": 1}})
    result = run_subagent({"runtime": runtime, "task": "inspect"}, role, "inspect")
    assert len(reports) == 1
    assert runtime.model_metrics["requests"] == 2
    assert result["metadata"]["tool_calls_used"] == 1
    assert result["ok"] is False and result["stop_reason"] == "tool_budget_exhausted"
    assert "no implementation done" in result["summary"]


def test_finalization_cannot_bypass_global_budget_or_execute_new_calls(tmp_path):
    from mokioclaw.core.tool_loop import ToolConversation
    from mokioclaw.providers.model_policy import RunBudgetExceeded
    from mokioclaw.providers.openai_provider import invoke_streaming

    class Model:
        def invoke(self, messages):
            return AIMessage(content="done")

    runtime = RuntimeState(workspace=tmp_path, run_request_limit=1)
    conversation = ToolConversation(
        [HumanMessage(content="task")],
        state={"runtime": runtime},
        writer=lambda _: None,
        node="test",
        invoke=invoke_streaming,
    )
    conversation.request(Model())
    with pytest.raises(RunBudgetExceeded):
        conversation.finish(Model(), "report")
    assert runtime.model_metrics["requests"] == 1


def test_verifier_gets_a_tool_free_decision_after_last_inspection(tmp_path, monkeypatch):
    from mokioclaw.graph import nodes

    (tmp_path / "a.py").write_text("observed\n")
    calls = []

    class Bound:
        def invoke(self, messages):
            calls.append(1)
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileReadTool",
                        "args": {"file_path": "a.py"},
                        "id": f"read-{len(calls)}",
                    }
                ],
            )

    class Model:
        def bind_tools(self, tools):
            return Bound()

        def invoke(self, messages):
            assert len([m for m in messages if isinstance(m, ToolMessage)]) == 8
            return AIMessage(
                content=json.dumps(
                    {
                        "passed": True,
                        "reason": "observed",
                        "checks": [{"name": "read", "passed": True}],
                        "recommended_next_instruction": "",
                    }
                )
            )

    monkeypatch.setattr(nodes, "create_model", Model)
    runtime = RuntimeState(workspace=tmp_path)
    result = nodes.verifier_node({"task": "inspect a.py", "runtime": runtime})
    assert len(calls) == 8 and runtime.model_metrics["requests"] == 9
    assert result["passed"] and result["attempts"] == 1


def test_finalization_discards_unexpected_writes_and_preserves_protocol(tmp_path):
    from mokioclaw.core.context_compaction import message_groups
    from mokioclaw.core.tool_loop import ToolConversation
    from mokioclaw.providers.openai_provider import invoke_streaming

    class IncorrectModel:
        def invoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "FileWriteTool",
                        "id": "late-write",
                        "args": {"file_path": "late.txt", "content": "must not execute"},
                    }
                ],
            )

    events = []
    messages = [HumanMessage(content="report")]
    conversation = ToolConversation(
        messages,
        state={"runtime": RuntimeState(workspace=tmp_path)},
        writer=events.append,
        node="test",
        invoke=invoke_streaming,
    )
    assert conversation.finish(IncorrectModel(), "summarize") is None
    assert not (tmp_path / "late.txt").exists()
    message_groups(messages)
    assert events[-1]["result"]["status"] == "not_executed"
