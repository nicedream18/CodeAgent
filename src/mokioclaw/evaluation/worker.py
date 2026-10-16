"""One fresh host process per trial. Only model transport runs on the host."""

from __future__ import annotations

import json
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from mokioclaw.evaluation.models import EvalTrialConfig

EXECUTION_TOOLS = frozenset(
    {
        "FileReadTool",
        "FileWriteTool",
        "FileEditTool",
        "FileGlobTool",
        "GrepTool",
        "BashTool",
        "LoadSkillTool",
        "NotepadReadTool",
        "NotepadAppendTool",
        "TodoUpdateTool",
    }
)


def initialize(runtime: Any, config: EvalTrialConfig, image: str, owner: str) -> None:
    from mokioclaw.agents.registry import SubagentRegistry, default_subagents

    runtime.run_id = owner
    runtime.run_control.run_id = owner
    runtime.evaluation_mode = True
    runtime.execution_backend = "docker"
    runtime.execution_image_digest = image
    runtime.docker_image = image
    runtime.workflow_mode = "full" if config.architecture == "baseline" else config.architecture
    runtime.context_token_limit = config.context_tokens
    runtime.model_context_reserve_tokens = config.output_tokens
    runtime.run_token_limit = config.token_limit
    runtime.run_request_limit = config.request_limit
    runtime.run_tool_limit = config.tool_limit
    runtime.bash_default_timeout_seconds = runtime.bash_max_timeout_seconds = config.bash_seconds
    runtime.bash_env_file = None
    runtime.tool_retry_limit = 0
    runtime.approval_mode = "auto"
    runtime.tool_policies = {
        "WebSearchTool": "deny",
        "ToolSearchTool": "deny",
        "MCP_*": "deny",
        "CallSearchAgentTool": "deny",
        "AgentResultTool": "deny",
        "AgentCancelTool": "deny",
    }
    runtime.mcp_servers = []
    runtime.mcp_loading = "deferred"
    runtime.mcp_loaded_tools = {}
    runtime.subagent_task_manager = None
    registry = SubagentRegistry()
    defaults = default_subagents()
    for name in ("repoAgent", "codeAgent"):
        registry.register(defaults[name])
    runtime.subagent_registry = registry
    runtime.subagent_configs = {}
    runtime.agent_profiles = {}
    runtime.settings = {
        "model_profiles": {
            "evaluation": {
                "model": config.model,
                "context_tokens": config.context_tokens,
                "output_tokens": config.output_tokens,
                "max_retries": 0,
            }
        }
    }
    runtime.model_profile = "evaluation"
    runtime.tool_cache.clear()
    if config.architecture == "baseline":
        runtime.active_agent_tools = set(EXECUTION_TOOLS)


def baseline_stream(task: str, **options: Any) -> Any:
    from langgraph.config import get_stream_writer
    from langgraph.graph import END, START, StateGraph

    from mokioclaw.agents.code_agent import run_code_agent
    from mokioclaw.graph.state import MokioGraphState

    lifecycle = options["_lifecycle"]

    def implement(state: MokioGraphState) -> dict[str, Any]:
        result = run_code_agent(state, task, writer=get_stream_writer(), max_loops=40)
        return {
            "final_answer": result["summary"],
            "messages": result["messages"],
            "todos": result["todos"],
        }

    graph = StateGraph(MokioGraphState)
    graph.add_node("baseline", implement)
    graph.add_edge(START, "baseline")
    graph.add_edge("baseline", END)
    from mokioclaw.core.agent import _graph_envelopes

    yield from cast(Callable[..., Any], _graph_envelopes)(
        graph.compile(), lifecycle.state, terminal=True
    )


def main(spec_path: Path) -> None:
    from mokioclaw.core.agent import _controlled_stream, stream_agent_events
    from mokioclaw.core.run_control import RunControl

    spec = json.loads(spec_path.read_text())
    config = EvalTrialConfig.model_validate(spec["config"])
    workspace = Path(spec["workspace"])
    control = RunControl()
    previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: control.cancel())
    control.runtime_initializer = lambda runtime: initialize(
        runtime, config, spec["image"], spec["owner"]
    )
    task = (
        spec["requirement"]
        + "\n\n"
        + (
            "评测环境：只修改允许的源代码，禁止修改测试、依赖和测试配置。隐藏测试在外部验收。"
            "可以新增源代码辅助文件；TODO.md/NOTEPAD.md/HISTORY_SUMMARY.md 为允许的过程记录。"
            "Bash 使用离线 Docker 和预装依赖，不要联网或装包；使用 PYTHONPATH=src:. python -m pytest 运行可见测试。"
            "不使用网页、MCP、自定义 hooks、持久授权或后台执行。最后如实说明实现与验证，最后一行用 AGENT_CLAIM: solved 或 AGENT_CLAIM: unsolved 标明自评。"
        )
    )
    options: dict[str, Any] = dict(
        workspace=workspace,
        max_attempts=3,
        approval_mode="auto",
        approval_handler=None,
        checkpoint_mode="light",
        resume_workspace=None,
        trace_mode="on",
        use_daemon=False,
        execution_backend="docker",
        workflow_mode=None,
        skill=None,
    )
    path: list[str] = []
    streams: list[str] = []
    calls: list[str] = []
    terminal: dict[str, Any] = {}
    claim: bool | None = None
    try:
        iterator = (
            cast(Callable[..., Any], _controlled_stream)(
                baseline_stream, task, options, None, session=False, execution_control=control
            )
            if config.architecture == "baseline"
            else stream_agent_events(task, **options, run_control=control)
        )
        with Path(spec["events"]).open("a") as handle:
            for envelope in iterator:
                event = envelope.get("event", {})
                if not isinstance(event, dict) or event.get("type") != "llm_token":
                    saved = envelope
                    if envelope.get("type") == "graph_event":
                        saved = {
                            "type": "graph_event",
                            "event": {
                                k: {"passed": v.get("passed")}
                                for k, v in event.items()
                                if isinstance(v, dict)
                            },
                        }
                    line = json.dumps(saved, ensure_ascii=False, default=str)
                    if len(line) > 16000:
                        line = json.dumps(
                            {
                                "type": envelope.get("type"),
                                "summary": line[:16000],
                                "truncated": True,
                            }
                        )
                    handle.write(line + "\n")
                    handle.flush()
                if event.get("type") in {"model_usage", "llm_request", "tool_result"}:
                    runtime = getattr(control, "runtime", None)
                    progress = {
                        "status": "running",
                        "model_metrics": getattr(runtime, "model_metrics", {}),
                        "tool_count": getattr(runtime, "tool_execution_count", 0),
                        "actual_path": path,
                        "stream_ids": streams,
                        "tool_call_ids": calls,
                        "run_id": getattr(runtime, "run_id", spec["owner"]),
                    }
                    progress_path = Path(spec["output"]).with_suffix(".progress.json")
                    temp_path = progress_path.with_suffix(".tmp")
                    temp_path.write_text(json.dumps(progress))
                    temp_path.replace(progress_path)
                if envelope.get("type") == "graph_event":
                    for node, update in event.items():
                        path.append(node)
                        if isinstance(update, dict) and isinstance(update.get("final_answer"), str):
                            if "AGENT_CLAIM: solved" in update["final_answer"]:
                                claim = True
                            elif "AGENT_CLAIM: unsolved" in update["final_answer"]:
                                claim = False
                        if isinstance(update, dict) and "passed" in update:
                            claim = bool(update["passed"])
                elif isinstance(event, dict):
                    if event.get("type") == "llm_stream_start":
                        streams.append(event["stream_id"])
                    if event.get("tool_call_id") and event["tool_call_id"] not in calls:
                        calls.append(event["tool_call_id"])
                    if event.get("type") in {
                        "workflow_mode",
                        "workflow_escalated",
                        "workflow_selected",
                    }:
                        path.append(str(event))
                    if event.get("type") == "run_finished":
                        terminal = event
    except BaseException as exc:
        # A worker failure is retained; it is never silently retried.
        terminal = {**terminal, "status": "failed", "worker_error": f"{type(exc).__name__}: {exc}"}
    finally:
        runtime = getattr(control, "runtime", None)
        output = {
            "status": terminal.get("status", "failed"),
            "stop_reason": terminal.get("stop_reason"),
            "error": terminal.get("worker_error"),
            "model_metrics": getattr(runtime, "model_metrics", {}),
            "tool_count": getattr(runtime, "tool_execution_count", 0),
            "actual_path": path,
            "stream_ids": streams,
            "tool_call_ids": calls,
            "model_claim": claim,
            "run_id": getattr(runtime, "run_id", spec["owner"]),
        }
        Path(spec["output"]).write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
        control.close()
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
