"""Offline pressure contracts. Synthetic models and estimates, never real A/B data."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from mokioclaw.core.checkpoint import CheckpointManager
from mokioclaw.core.context_budget import ContextBudget, ContextBudgetError, snapshot
from mokioclaw.core.context_control import ContextControl
from mokioclaw.core.loop_guard import guard_for
from mokioclaw.core.run_control import RunControl
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_loop import ToolConversation
from mokioclaw.core.trace import TraceRecorder
from mokioclaw.providers.openai_provider import invoke_streaming
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.registry import build_tool


class PressureModel:
    """Fixed decisions, with final artifacts derived from visible tool evidence."""

    def __init__(self, scenario: str, *, summary: bool = False) -> None:
        self.scenario, self.summary, self.step = scenario, summary, 0
        self.inputs: list[int] = []
        self.kwargs: dict[str, Any] = {
            "tools": [{"name": "FileReadTool", "schema": "fixed-local-schema"}]
        }

    def get_num_tokens_from_messages(self, messages: list[Any]) -> int:
        return sum(32 + (len(str(m.content).encode()) + 3) // 4 for m in messages)

    def bind(self, **kwargs: Any) -> PressureModel:
        return self

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.inputs.append(self.get_num_tokens_from_messages(messages))
        if self.summary:
            return AIMessage(
                content='{"summary":"Keep task, failure evidence, output references and unfinished verification; reread missing tail evidence."}'
            )
        self.step += 1
        n = self.step
        latest: dict[str, Any] = next(
            (json.loads(str(m.content)) for m in reversed(messages) if isinstance(m, ToolMessage)),
            {},
        )

        def call(name: str, args: dict[str, Any]) -> AIMessage:
            return AIMessage(
                content="", tool_calls=[{"id": f"pressure-{n}", "name": name, "args": args}]
            )

        if self.scenario == "long_file":
            if n <= 10:
                return call(
                    "FileReadTool",
                    {
                        "file_path": "requirements.txt",
                        "char_offset": (n - 1) * 12000,
                        "char_limit": 12000,
                    },
                )
            if n == 11:
                return call(
                    "FileReadTool",
                    {
                        "file_path": "requirements.txt",
                        "char_offset": int(latest["total_characters"]) - 100,
                        "char_limit": 100,
                    },
                )
            if n == 12:
                requirement = re.search(r"OUTPUT=(\w+)", str(latest.get("content", "")))
                return call(
                    "FileWriteTool",
                    {
                        "file_path": "answer.txt",
                        "content": requirement.group(1) if requirement else "MISSING_EVIDENCE",
                    },
                )
        elif self.scenario == "command_tail":
            if n <= 12:
                code = f"print('diagnostic-{n} ' * 18000); print('E_TAIL:{n}'); raise SystemExit(2)"
                return call(
                    "BashTool", {"command": "python -c " + shlex.quote(code), "timeout_seconds": 10}
                )
            if n == 13:
                error = re.search(r"E_TAIL:\d+", str(latest.get("stdout", "")))
                return call(
                    "FileWriteTool",
                    {
                        "file_path": "answer.txt",
                        "content": error.group(0) if error else "MISSING_TAIL",
                    },
                )
        else:
            if n <= 20 and n % 2:
                return call(
                    "FileWriteTool", {"file_path": "candidate.txt", "content": f"broken-{n}"}
                )
            if n <= 20:
                code = f"from pathlib import Path; print('failed-check-{n} ' * 12000); print('FAIL:{n}'); assert Path('candidate.txt').read_text() == 'fixed'"
                return call(
                    "BashTool", {"command": "python -c " + shlex.quote(code), "timeout_seconds": 10}
                )
            if n == 21:
                return call("FileWriteTool", {"file_path": "candidate.txt", "content": "fixed"})
            if n == 22:
                code = "from pathlib import Path; assert Path('candidate.txt').read_text() == 'fixed'; print('CHECK_FIXED')"
                return call(
                    "BashTool", {"command": "python -c " + shlex.quote(code), "timeout_seconds": 10}
                )
            if n == 23:
                current = any("OUTPUT=corrected_value" in str(m.content) for m in messages)
                return call(
                    "FileWriteTool",
                    {
                        "file_path": "answer.txt",
                        "content": "corrected_value" if current else "STALE_REQUIREMENT",
                    },
                )
        return AIMessage(
            content="Inspect artifacts and independent assertions; this is an offline control test."
        )


def run_case(
    workspace: Path,
    scenario: str,
    *,
    enabled: bool,
    manual: bool = False,
    execution_backend: str = "local",
    docker_image: str = "",
) -> dict[str, Any]:
    workspace.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeState(
        workspace=workspace.resolve(),
        context_token_limit=16384,
        model_context_reserve_tokens=1024,
        context_safety_tokens=512,
        context_compression_enabled=enabled,
        context_control=ContextControl(),
        run_control=RunControl(),
        approval_mode="auto",
        execution_backend=execution_backend,
        docker_image=docker_image or "mokioclaw-execution:0.2.0",
    )
    # Distinct ranges and actual file mutations are valid progress; recovery
    # credit must nevertheless survive every automatic/manual compaction.
    guard_for(runtime).recover()
    guard_for(runtime).observe("0" * 64, "FixturePriorObservation", {"ok": True})
    state: dict[str, Any] = {
        "runtime": runtime,
        "task": f"{scenario}: produce answer.txt from observed evidence",
        "acceptance_criteria": ["Independent artifact assertion passes"],
        "todos": [{"id": "verify", "status": "pending", "content": "verify answer"}],
        "last_error": "",
        "messages": [],
    }
    runtime.foreground_state = state
    runtime.run_id = "pressure-" + uuid4().hex[:12]
    trace = TraceRecorder(runtime, task=state["task"])
    trace.start(state)
    events: list[dict[str, Any]] = []

    def writer(event: dict[str, Any]) -> None:
        events.append(event)
        trace.record_custom_event(event)

    messages: list[Any] = [
        SystemMessage(content="Use local tools; preserve task, failures and requirements."),
        HumanMessage(content=state["task"]),
    ]
    state["messages"] = messages
    model, summary = PressureModel(scenario), PressureModel(scenario, summary=True)
    conversation = ToolConversation(
        messages, state=state, writer=writer, node="codeAgent", invoke=invoke_streaming
    )
    if scenario == "long_file":
        (workspace / "requirements.txt").write_text(
            "".join(f"line-{i:06d}: irrelevant material. " for i in range(12000))
            + "\nOUTPUT=tail_evidence_value\n"
        )
        # Actual result clipping as well as range-limited reads.
        initial = execute_tool(
            runtime,
            build_tool(runtime, "FileReadTool"),
            {"file_path": "requirements.txt"},
            on_event=writer,
            tool_call_id="initial",
        )
        messages.extend(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "initial",
                            "name": "FileReadTool",
                            "args": {"file_path": "requirements.txt"},
                        }
                    ],
                ),
                ToolMessage(content=json.dumps(initial), tool_call_id="initial"),
            ]
        )
    input_estimates: list[int] = []
    manual_requested, corrected = False, False
    status, stop_reason = "finished", ""
    try:
        with patch("mokioclaw.providers.openai_provider.create_model", return_value=summary):
            for _ in range(40):
                budget = ContextBudget.from_runtime(runtime)
                before = snapshot(model, messages, budget).input_tokens
                input_estimates.append(before)
                if manual and not manual_requested and before >= budget.high:
                    runtime.context_control.request()
                    manual_requested = True
                response = conversation.request(model)
                calls = response.tool_calls
                if not calls:
                    break
                with conversation.batch(calls) as pending:
                    for call in pending:
                        writer(
                            {
                                "type": "tool_call",
                                "node": "codeAgent",
                                "name": call["name"],
                                "tool_call_id": call["id"],
                                "args": call["args"],
                            }
                        )
                        result = execute_tool(
                            runtime,
                            build_tool(runtime, call["name"]),
                            call["args"],
                            on_event=writer,
                            tool_call_id=call["id"],
                        )
                        writer(
                            {
                                "type": "tool_result",
                                "node": "codeAgent",
                                "name": call["name"],
                                "tool_call_id": call["id"],
                                "result": result,
                            }
                        )
                        conversation.append(
                            ToolMessage(
                                content=json.dumps(result, ensure_ascii=False),
                                name=call["name"],
                                tool_call_id=call["id"],
                            )
                        )
                        if call["name"] == "BashTool" and not result.get("ok"):
                            state["last_error"] = str(result.get("stdout", ""))[-180:]
                if (
                    scenario == "repair_correction"
                    and not corrected
                    and any(
                        e.get("type") == "context_compression" and e.get("status") == "applied"
                        for e in events
                    )
                ):
                    runtime.run_control.inbox.submit(
                        "pressure-correction",
                        "Do not modify database. OUTPUT=corrected_value; verify candidate.txt.",
                    )
                    runtime.run_control.inbox.apply(state, writer)
                    messages.append(
                        HumanMessage(
                            content="Do not modify database. OUTPUT=corrected_value; verify candidate.txt."
                        )
                    )
                    state["messages"] = messages
                    corrected = True
            else:
                raise AssertionError("fixed pressure model exceeded its finite script")
    except ContextBudgetError:
        status, stop_reason = "failed", "context_budget_exceeded"
        state.update(passed=False, stop_reason=stop_reason)
    finally:
        runtime.run_control.close()
    compression = [
        e for e in events if e.get("type") == "context_compression" and e.get("status") == "applied"
    ]
    expected = {
        "long_file": "tail_evidence_value",
        "command_tail": "E_TAIL:12",
        "repair_correction": "corrected_value",
    }[scenario]
    answer = workspace / "answer.txt"
    solved = answer.exists() and answer.read_text() == expected
    if scenario == "repair_correction":
        solved = (
            solved
            and (workspace / "candidate.txt").read_text() == "fixed"
            and not (workspace / "database.db").exists()
            and corrected
        )
    log_paths = [
        e.get("full_output_path")
        for e in events
        if e.get("type") == "tool_output_truncated" and e.get("full_output_path")
    ]
    state["passed"] = bool(solved)
    CheckpointManager(runtime).save(state, status=status)
    trace.end(status=status, latest_node="codeAgent", final_state=state)
    return {
        "schema_version": 1,
        "simulation": True,
        "real_model": False,
        "scenario": scenario,
        "enabled": enabled,
        "manual": manual,
        "execution_status": status,
        "stop_reason": stop_reason,
        "artifact_passed": bool(solved),
        "budget": {
            "window": 16384,
            "output": 1024,
            "safety": 512,
            "high": ContextBudget.from_runtime(runtime).high,
            "hard": ContextBudget.from_runtime(runtime).input_limit,
        },
        "estimated_inputs_before_governance": input_estimates,
        "actual_request_input_estimates": model.inputs,
        "compression_count": len(compression),
        "compressions": compression,
        "summary_input_estimates": summary.inputs,
        "summary_overhead_requests": len(summary.inputs),
        "tool_calls": runtime.tool_execution_count,
        "recoveries_used": guard_for(runtime).recoveries,
        "guard_history_count": len(guard_for(runtime).history),
        "prior_history_retained": any(h["call"] == "0" * 64 for h in guard_for(runtime).history),
        "log_references": log_paths,
        "correction_applied": corrected,
        "model_metrics": runtime.model_metrics,
        "checkpoint": ".mokioclaw/checkpoints/checkpoint.json",
        "trace": str(trace.root.relative_to(runtime.workspace)),
        "run_id": runtime.run_id,
        "execution_backend": execution_backend,
        "image_digest": runtime.execution_image_digest,
    }


def run_suite(
    root: Path, *, execution_backend: str = "local", docker_image: str = ""
) -> dict[str, Any]:
    cases = [
        run_case(
            root / f"{scenario}-{enabled}-{manual}",
            scenario,
            enabled=enabled,
            manual=manual,
            execution_backend=execution_backend,
            docker_image=docker_image,
        )
        for scenario in ("long_file", "command_tail", "repair_correction")
        for enabled in (True, False)
        for manual in (False, True)
    ]
    return {
        "version": 1,
        "simulation": True,
        "real_model": False,
        "cases": cases,
        "passed": all(
            (
                c["artifact_passed"] and c["compression_count"] >= 1
                if c["enabled"]
                else c["execution_status"] == "failed"
                and c["stop_reason"] == "context_budget_exceeded"
                and not c["artifact_passed"]
            )
            and c["recoveries_used"] == 1
            for c in cases
        ),
    }
