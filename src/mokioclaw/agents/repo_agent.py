"""Read-only repository exploration through the normal execution boundary."""

import json

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.providers.model_policy import create_role_model
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tool


def run_repo_agent(state, instruction, *, writer=None, max_loops=8):
    writer = writer or (lambda event: None)
    runtime = state["runtime"]
    tools = [
        build_tool(runtime, name)
        for name in ("FileReadTool", "GrepTool", "FileGlobTool")
        if runtime.active_agent_tools is None or name in runtime.active_agent_tools
    ]
    base_model = create_role_model(create_model, state["runtime"], "repoAgent")
    model = base_model.bind_tools(tools)
    messages = [
        SystemMessage(
            content="Explore the repository read-only before implementation. Trace entry points, call chains, conventions and tests. Return JSON with summary, entry_points, call_chains, conventions, suggested_changes, tests, evidence, and unresolved. Cite only observed files and lines. Never edit files. Return at most ten key files."
        ),
        HumanMessage(content=f"Task: {state['task']}\nInstruction: {instruction}"),
    ]
    evidence = []
    conversation = ToolConversation(
        messages,
        state=state,
        writer=writer,
        node="repoAgent",
        invoke=invoke_streaming,
        produced=None,
    )
    needs_summary = False
    for _ in range(max_loops):
        active = runtime.active_subagent_budget
        if active is not None and active.used >= active.limit:
            needs_summary = True
            break
        response = conversation.request(model)
        tool_calls = getattr(response, "tool_calls", None) or []
        if not getattr(response, "tool_calls", None):
            break
        with conversation.batch(tool_calls) as pending_calls:
            for call in pending_calls:
                tool = next((t for t in tools if t.name == call["name"]), None)
                result = (
                    execute_tool(
                        runtime,
                        tool,
                        call["args"],
                        tool_call_id=call["id"],
                        on_event=writer,
                    )
                    if tool
                    else {
                        "ok": False,
                        "error": "tool outside repository exploration scope",
                    }
                )
                messages.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id=call["id"],
                        name=call["name"],
                    )
                )
                writer(
                    {
                        "type": "tool_result",
                        "node": "repoAgent",
                        "name": call["name"],
                        "tool_call_id": call["id"],
                        "result": result,
                    }
                )
                if result.get("ok"):
                    for match in result.get("matches", []):
                        evidence.append(
                            {
                                "kind": "file",
                                "label": match["path"],
                                "reference": f"{match['path']}:{match['line']}",
                            }
                        )
                    if call["name"] == "FileReadTool":
                        evidence.append(
                            {
                                "kind": "file",
                                "label": result.get("path", ""),
                                "reference": f"{result.get('path', '')}:{int(call['args'].get('offset', 0)) + 1}",
                            }
                        )
        if active is not None and active.exhausted:
            needs_summary = True
            break
    else:
        needs_summary = True
    if needs_summary:
        conversation.finish(
            base_model,
            "Repository exploration has ended; no more tools are available in this handoff. "
            "Return the requested concise JSON now: observed entry points, conventions, tests, "
            "suggested edit locations and unresolved questions. Do not reproduce entire source files "
            "or claim implementation is complete. The code agent can read exact source before editing.",
        )
    raw = str(messages[-1].content)
    try:
        payload = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
        if not isinstance(payload, dict):
            payload = {"summary": raw}
    except ValueError:
        payload = {"summary": raw}
    payload.setdefault("summary", raw)
    payload["evidence"] = evidence
    payload["state_updates"] = {
        "repo_context": json.dumps(
            {
                key: payload[key]
                for key in (
                    "entry_points",
                    "suggested_changes",
                    "tests",
                    "summary",
                    "call_chains",
                    "conventions",
                    "unresolved",
                )
                if key in payload
            },
            ensure_ascii=False,
        ),
        "repo_explored": True,
    }
    return payload
