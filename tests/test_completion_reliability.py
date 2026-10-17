import json

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
        api_key="offline-fixture", model="unknown-compatible-model",
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
            AIMessage(content="", tool_calls=[{"name": "FileReadTool", "args": {}, "id": str(index)}]),
            ToolMessage(content=content, tool_call_id=str(index)),
        ]
    runtime = RuntimeState(workspace=tmp_path)
    state = {"runtime": runtime, "task": task, "repo_explored": True, "repo_context": "observed map"}
    budget = ContextBudget(window=3500, output=200, safety=100, target_ratio=0.3)
    replacement, event = compact_messages(
        Counter(), messages, state, budget=budget, summarizer=lambda *_: {"summary": "short"}
    )
    assert event["status"] == "applied" and not event["target_reached"]
    assert event["after_tokens"] <= budget.input_limit
    assert any(isinstance(m, ToolMessage) and m.content == latest for m in replacement)
    summary = next(json.loads(m.content) for m in replacement if isinstance(m, AIMessage) and not m.tool_calls)
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
