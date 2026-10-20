"""Independent check execution; pass/fail comes from observed tool results."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.run_control import check_run
from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.core.tool_output import bound_tool_result
from mokioclaw.providers.model_policy import create_role_model
from mokioclaw.providers.openai_provider import create_model, invoke_streaming
from mokioclaw.tools.execution import execute_tool, reserve_subagent_tool_call, tool_failure_result
from mokioclaw.tools.registry import build_tool

TOOLS = frozenset({"FileReadTool", "FileGlobTool", "GrepTool", "BashTool"})
PROMPT = """You are testAgent, an independent testing specialist after implementation.
Inspect the changed code, existing tests and acceptance criteria. Run the provided
verification commands when relevant, then the smallest useful regression checks.
Use exact tool names. Inspect project configuration before guessing a test command.
Do not edit implementation, tests, assertions or test configuration. Do not install
dependencies or start background commands. Shell commands are for foreground checks;
normal test caches and reports are allowed. Request codeAgent to add missing tests
or repair code. A command's exit code is evidence, never substitute a success message
or mask it with a pipe, '|| true', or unconditional exit 0. Read bounded failure logs
when necessary. End with a concise report of commands, actual failures, coverage gaps
and recommended fixes. Do not repeat a check whose code and inputs are unchanged.
The runtime determines check status from observed results; you do not grant acceptance.
"""


def run_test_agent(
    state: dict[str, Any],
    instruction: str,
    *,
    writer: Callable[[dict[str, Any]], None] | None = None,
    max_loops: int = 8,
) -> dict[str, Any]:
    runtime = state["runtime"]
    check_run(runtime)
    writer = writer or (lambda _: None)
    names = TOOLS if runtime.active_agent_tools is None else TOOLS & runtime.active_agent_tools
    tools = {name: build_tool(runtime, name) for name in sorted(names)}
    model = create_role_model(create_model, runtime, "testAgent")  # type: ignore[no-untyped-call]
    bound = model.bind_tools(list(tools.values()))
    context = {
        key: state.get(key)
        for key in (
            "task",
            "repo_context",
            "code_agent_summary",
            "acceptance_criteria",
            "verification_commands",
            "latest_user_corrections",
            "test_report",
        )
    }
    messages: list[Any] = [
        SystemMessage(content=PROMPT),
        HumanMessage(content=instruction + "\n" + json.dumps(context, ensure_ascii=False)),
    ]
    conversation = ToolConversation(
        messages,
        state=state,
        writer=writer,
        node="testAgent",
        invoke=invoke_streaming,
        report_model=model,
    )
    commands: list[dict[str, Any]] = []
    errors: list[str] = []
    state["test_report"] = {
        "status": "incomplete",
        "commands": commands,
        "errors": errors,
        "basis": "observed_tool_results",
    }
    if runtime.foreground_state is not None:
        runtime.foreground_state["test_report"] = state["test_report"]
        runtime.foreground_state["testing_required"] = True
    ended = False
    for _ in range(max_loops):
        response = conversation.request(bound)
        calls = getattr(response, "tool_calls", None) or []
        if not calls:
            ended = True
            break
        with conversation.batch(calls) as pending:
            for call in pending:
                name, args = call["name"], call.get("args") or {}
                writer(
                    {
                        "type": "tool_call",
                        "node": "testAgent",
                        "name": name,
                        "args": args,
                        "tool_call_id": call["id"],
                    }
                )
                if name in tools and not (name == "BashTool" and args.get("run_in_background")):
                    result = execute_tool(
                        runtime,
                        tools[name],
                        args,
                        tool_call_id=call["id"],
                        on_event=writer,
                        on_retry=writer,
                    )
                else:
                    result = reserve_subagent_tool_call(runtime, name, call["id"], on_event=writer)
                    if result is None:
                        result = tool_failure_result("tool outside testing scope", tool_name=name)
                result = bound_tool_result(result, runtime, tool_name=name, writer=writer)
                if name == "BashTool":
                    commands.append(
                        {
                            "command": str(args.get("command", "")),
                            "tool_call_id": call["id"],
                            "ok": result.get("ok", False),
                            "exit_code": result.get("exit_code"),
                            "timed_out": result.get("timed_out", False),
                            "stdout": str(result.get("stdout", ""))[:2400],
                            "stderr": str(result.get("stderr") or result.get("error", ""))[:2400],
                            "stdout_path": result.get(
                                "stdout_full_output_path", result.get("stdout_path", "")
                            ),
                            "stderr_path": result.get(
                                "stderr_full_output_path", result.get("stderr_path", "")
                            ),
                        }
                    )
                if not result.get("ok"):
                    errors.append(str(result.get("error") or f"{name} failed")[:400])
                conversation.append(
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        name=name,
                        tool_call_id=call["id"],
                    )
                )
                writer(
                    {
                        "type": "tool_result",
                        "node": "testAgent",
                        "name": name,
                        "tool_call_id": call["id"],
                        "result": result,
                    }
                )
        if conversation.yielded:
            break
        budget = runtime.active_subagent_budget
        if budget is not None and budget.exhausted:
            break
    if not ended and not conversation.yielded:
        conversation.finish(
            model, "No further tools. Report observed checks and unfinished testing."
        )
    failed = any(isinstance(c["exit_code"], int) and c["exit_code"] != 0 for c in commands)
    incomplete = bool(
        errors
        or not ended
        or conversation.yielded
        or any(type(c["exit_code"]) is not int or c["timed_out"] for c in commands)
    )
    status = (
        "failed" if failed else "incomplete" if incomplete else "passed" if commands else "not_run"
    )
    summary = str(messages[-1].content)[:4000] if messages else ""
    report = {
        "status": status,
        "summary": summary,
        "commands": commands,
        "errors": errors,
        "basis": "observed_tool_results",
    }
    # Save partial facts before registry/control exceptions can hand back progress.
    state["test_report"] = report
    state["testing_required"] = False
    if runtime.foreground_state is not None:
        runtime.foreground_state.update(test_report=report, testing_required=False)
    return {
        "ok": status in {"passed", "not_run"},
        "summary": summary,
        "stop_reason": None if status in {"passed", "not_run"} else "testing_" + status,
        "unresolved": errors + ([] if status == "passed" else ["Testing status: " + status]),
        "evidence": [
            {
                "kind": "command",
                "label": c["command"],
                "reference": c["tool_call_id"],
                "details": json.dumps(c, ensure_ascii=False),
            }
            for c in commands
        ],
        "state_updates": {"test_report": report, "testing_required": False},
    }
