from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, message_to_dict

from mokioclaw.core.artifacts import DiagnosticArtifact

VALID_TRACE_MODES = {"on", "off"}
TRACE_ROOT = Path(".mokioclaw") / "traces"
EVENTS_FILE = "events.jsonl"
SUMMARY_FILE = "summary.json"
TIMELINE_FILE = "timeline.md"
MAX_PAYLOAD_TEXT = 1200
TIMELINE_HEAD_ITEMS = 40
TIMELINE_TAIL_ITEMS = 80


def normalize_trace_mode(mode: str | None) -> str:
    normalized = (mode or "on").strip().lower()
    return normalized if normalized in VALID_TRACE_MODES else "on"


def _serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return call


class TraceRecorder:
    def __init__(self, runtime: Any, task: str = "") -> None:
        self._lock = threading.RLock()
        self.runtime = runtime
        self.workspace = runtime.workspace
        self.mode = normalize_trace_mode(getattr(runtime, "trace_mode", "on"))
        self.trace_id = getattr(runtime, "trace_id", None) or _new_trace_id()
        self.task = task
        self.root = self.workspace / TRACE_ROOT / self.trace_id
        self.started_at = time.perf_counter()
        self.started_at_iso = utc_now()
        self.sequence = 0
        self.errors: list[str] = []
        self.status = "running"
        self.node_visits: dict[str, int] = {}
        self.tool_calls = 0
        self.failed_tool_calls = 0
        self.approval_count = 0
        self.permission_decisions = {"requested": 0, "granted": 0, "denied": 0}
        self.permission_call_ids: set[str] = set()
        self.tool_retries = 0
        self.tool_retry_errors: dict[str, int] = {}
        self.tool_error_types: dict[str, int] = {}
        self.tool_retry_stops = {
            "retry_limit_reached": 0,
            "non_idempotent_side_effect": 0,
        }
        self.skill_load_successes = 0
        self.skill_load_failures = 0
        self.skill_names: set[str] = set()
        self.checkpoint_count = 0
        self.handoff_count = 0
        self.subagent_budget_exhaustions = 0
        self.subagent_result_status = {"ok": 0, "failed": 0}
        self.background_subagents: dict[str, dict[str, Any]] = {}
        self.context_metrics = {
            "compactions": 0,
            "truncations": 0,
            "failures": 0,
            "max_input_tokens": 0,
        }
        self.context_methods: set[str] = set()
        self.context_event_ids: set[str] = set()
        self.handoffs: dict[str, dict[str, Any]] = {}
        self.final_status = ""
        self.timeline_head: list[str] = []
        self.timeline_tail: list[str] = []
        self.timeline_omitted = 0
        self.stream_buffers: dict[str, DiagnosticArtifact] = {}
        self.stream_nodes: dict[str, str] = {}
        if self.enabled:
            runtime.trace_id = self.trace_id
            self.root.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def start(
        self,
        inputs: dict[str, Any],
        *,
        resumed: bool = False,
        resume_event: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        self.record(
            "run_start",
            {
                "task": inputs.get("task", self.task),
                "workspace": str(self.workspace),
                "resumed": resumed,
                "resume": resume_event or {},
                "max_attempts": inputs.get("max_attempts"),
                "checkpoint_mode": getattr(self.runtime, "checkpoint_mode", ""),
            },
        )

    @_serialized
    def record_emitted_event(self, event):
        """Persist before LangGraph can discard queued events on cancellation."""
        if not self.enabled:
            return
        self.record_custom_event(event)
        event["trace_id"] = self.trace_id
        event["trace_seq"] = self.sequence

    @_serialized
    def record_custom_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        if event.get("trace_id") == self.trace_id and "trace_seq" in event:
            return
        event_type = str(event.get("type", "custom_event"))
        if event_type in {
            "context_budget",
            "context_compression",
            "context_compression_failed",
            "tool_output_truncated",
        }:
            event_id = event.get("event_id")
            if event_id and event_id in self.context_event_ids:
                return
            if event_id:
                self.context_event_ids.add(event_id)
            if event_type == "context_budget":
                self.context_metrics["max_input_tokens"] = max(
                    self.context_metrics["max_input_tokens"],
                    int(event.get("input_tokens", 0)),
                )
                self.context_methods.add(str(event.get("estimation_method", "unknown")))
            elif (
                event_type == "context_compression"
                and event.get("status", "applied") == "applied"
            ):
                self.context_metrics["compactions"] += 1
            elif event_type == "context_compression_failed":
                self.context_metrics["failures"] += 1
            elif event_type == "tool_output_truncated":
                self.context_metrics["truncations"] += 1
        if event_type == "tool_result" and event.get("name") == "LoadSkillTool":
            result = event.get("result")
            if isinstance(result, dict):
                event = {
                    **event,
                    "result": {
                        key: result.get(key)
                        for key in (
                            "ok",
                            "name",
                            "source",
                            "path",
                            "error_type",
                            "diagnostic",
                        )
                        if key in result
                    },
                }
            else:
                event = {**event, "result": "[Skill load result redacted]"}
        stream_id = str(event.get("stream_id") or event.get("node", "model"))
        if event_type == "llm_stream_start":
            if stream_id in self.stream_buffers:
                self.record_custom_event(
                    {
                        "type": "llm_stream_end",
                        "stream_id": stream_id,
                        "status": "failed",
                    }
                )
            self.stream_buffers[stream_id] = DiagnosticArtifact(
                self.workspace, "model-stream"
            )
            self.stream_nodes[stream_id] = str(event.get("node", "model"))
        elif event_type == "llm_token":
            if stream_id not in self.stream_buffers:
                self.stream_buffers[stream_id] = DiagnosticArtifact(
                    self.workspace, "model-stream"
                )
                self.stream_nodes[stream_id] = str(event.get("node", "model"))
            self.stream_buffers[stream_id].write(
                str(event.get("text", "")).encode("utf-8")
            )
            return
        elif event_type == "llm_stream_end":
            artifact = self.stream_buffers.pop(stream_id, None)
            self.stream_nodes.pop(stream_id, None)
            if artifact is not None:
                event = {
                    **event,
                    **artifact.finish(),
                    "text": artifact.tail.decode("utf-8", errors="replace"),
                }
        if event_type == "tool_retry":
            self.tool_retries += 1
            error_type = str(event.get("error_type", "tool_error"))
            self.tool_retry_errors[error_type] = (
                self.tool_retry_errors.get(error_type, 0) + 1
            )
        elif event_type == "tool_call":
            self.tool_calls += 1
        elif event_type == "tool_result":
            result = event.get("result")
            if isinstance(result, dict):
                if result.get("ok") is False:
                    self.failed_tool_calls += 1
                    error_type = str(result.get("error_type", "tool_error"))
                    self.tool_error_types[error_type] = (
                        self.tool_error_types.get(error_type, 0) + 1
                    )
                stop_reason = str(result.get("retry_stop_reason", ""))
                if stop_reason in self.tool_retry_stops:
                    self.tool_retry_stops[stop_reason] += 1
                call_id = str(event.get("tool_call_id", ""))
                if (
                    result.get("requires_approval")
                    and call_id not in self.permission_call_ids
                ):
                    self.approval_count += 1
        elif event_type == "permission_requested":
            self.permission_decisions["requested"] += 1
            self.approval_count += 1
            self.permission_call_ids.add(str(event.get("tool_call_id", "")))
        elif event_type == "permission_granted":
            self.permission_decisions["granted"] += 1
            self.permission_call_ids.add(str(event.get("tool_call_id", "")))
        elif event_type == "permission_denied":
            self.permission_decisions["denied"] += 1
            self.permission_call_ids.add(str(event.get("tool_call_id", "")))
        elif event_type == "handoff":
            self.handoff_count += 1
            handoff_id = str(event.get("handoff_id", ""))
            if handoff_id:
                self.handoffs[handoff_id] = {
                    "handoff_id": handoff_id,
                    "subagent": event.get("to", ""),
                    "instruction": event.get("instruction", event.get("task", "")),
                    "status": "running",
                }
        elif event_type == "handoff_result":
            handoff_id = str(event.get("handoff_id", ""))
            ok = bool(event.get("ok", True))
            self.subagent_result_status["ok" if ok else "failed"] += 1
            if handoff_id:
                self.handoffs.setdefault(handoff_id, {"handoff_id": handoff_id})
                self.handoffs[handoff_id].update(
                    {
                        "subagent": event.get("from", ""),
                        "status": "ok" if ok else "failed",
                        "stop_reason": event.get("stop_reason"),
                    }
                )
        elif event_type == "subagent_budget_exhausted":
            self.subagent_budget_exhaustions += 1
        elif event_type in {"subagent_task_queued", "subagent_task_status"}:
            run_id = str(event.get("subagent_run_id", ""))
            if run_id:
                self.background_subagents[run_id] = {
                    "subagent_run_id": run_id,
                    "parent_run_id": event.get("parent_run_id", ""),
                    "handoff_id": event.get("handoff_id", ""),
                    "agent": event.get("agent", ""),
                    "status": event.get("status", "queued"),
                }
        elif event_type == "skill_loaded":
            self.skill_load_successes += 1
            name = str(event.get("skill_name", ""))
            if name:
                self.skill_names.add(name)
        elif event_type == "skill_load_failed":
            self.skill_load_failures += 1
        elif event_type == "checkpoint_saved":
            self.checkpoint_count += 1
        elif event_type == "checkpoint_resumed":
            self._timeline(
                f"resume {event.get('mode', '')} fallback={event.get('fallback', False)}"
            )

        self.record(
            f"custom:{event_type}",
            {
                "event_type": event_type,
                "node": event.get("node") or event.get("from") or "",
                "name": event.get("name") or "",
                "payload": compact_payload(event),
            },
        )

    @_serialized
    def record_graph_update(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        nodes = list(event.keys()) if isinstance(event, dict) else []
        for node in nodes:
            self.node_visits[node] = self.node_visits.get(node, 0) + 1
            if node == "final":
                update = event.get(node)
                if isinstance(update, dict):
                    self.final_status = (
                        "passed"
                        if "PASSED" in str(update.get("final_answer", ""))
                        else "failed"
                    )
        self.record(
            "graph_update",
            {
                "nodes": nodes,
                "payload": compact_payload(event),
            },
        )

    @_serialized
    def end(
        self,
        *,
        status: str,
        latest_node: str = "",
        final_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        if self.status != "running":
            return self.write_summary()
        for stream_id in list(self.stream_buffers):
            self.record_custom_event(
                {
                    "type": "llm_stream_end",
                    "stream_id": stream_id,
                    "node": self.stream_nodes.get(stream_id, "model"),
                    "status": "interrupted" if status == "interrupted" else "failed",
                    "reason": "run_ended_before_stream_end",
                }
            )
        self.status = status
        payload = {
            "status": status,
            "latest_node": latest_node,
            "attempts": (final_state or {}).get("attempts"),
            "passed": (final_state or {}).get("passed"),
            "final_status": self.final_status,
        }
        self.record("run_end", payload)
        return self.write_summary()

    @_serialized
    def record(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self.sequence += 1
            line = {
                "seq": self.sequence,
                "timestamp": utc_now(),
                "elapsed_ms": self.elapsed_ms(),
                "type": event_type,
                "payload": compact_payload(payload),
            }
            with (self.root / EVENTS_FILE).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
            self._timeline(format_timeline_line(line))
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")

    def write_summary(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        summary = self.summary_payload()
        try:
            write_json(self.root / SUMMARY_FILE, summary)
            (self.root / TIMELINE_FILE).write_text(
                build_timeline_markdown(summary, self.timeline_items()),
                encoding="utf-8",
            )
        except Exception as exc:
            self.errors.append(f"{type(exc).__name__}: {exc}")
            summary = self.summary_payload()
        return trace_summary_event(summary)

    def summary_payload(self) -> dict[str, Any]:
        manager = getattr(self.runtime, "subagent_task_manager", None)
        parent_run_id = getattr(self.runtime, "parent_run_id", None)
        if manager is not None and parent_run_id:
            for task in manager.list_for_parent(parent_run_id):
                run_id = str(task.get("subagent_run_id", ""))
                if run_id:
                    self.background_subagents[run_id] = {
                        "subagent_run_id": run_id,
                        "parent_run_id": task.get("parent_run_id", ""),
                        "handoff_id": task.get("handoff_id", ""),
                        "agent": task.get("agent", ""),
                        "status": task.get("status", "unknown"),
                    }
        return {
            "trace_id": self.trace_id,
            "status": self.status,
            "workspace": str(self.workspace),
            "trace_dir": str(self.root),
            "events_file": str(self.root / EVENTS_FILE),
            "summary_file": str(self.root / SUMMARY_FILE),
            "timeline_file": str(self.root / TIMELINE_FILE),
            "started_at": self.started_at_iso,
            "ended_at": utc_now(),
            "duration_ms": self.elapsed_ms(),
            "event_count": self.sequence,
            "node_visits": dict(sorted(self.node_visits.items())),
            "tool_calls": self.tool_calls,
            "failed_tool_calls": self.failed_tool_calls,
            "tool_retries": self.tool_retries,
            "tool_retry_errors": dict(sorted(self.tool_retry_errors.items())),
            "tool_error_types": dict(sorted(self.tool_error_types.items())),
            "tool_retry_stops": dict(self.tool_retry_stops),
            "approval_count": self.approval_count,
            "permission_decisions": dict(self.permission_decisions),
            "checkpoint_count": self.checkpoint_count,
            "handoff_count": self.handoff_count,
            "subagent_budget_exhaustions": self.subagent_budget_exhaustions,
            "subagent_result_status": dict(self.subagent_result_status),
            "background_subagent_total": len(self.background_subagents),
            "context_governance": {
                **self.context_metrics,
                "estimation_methods": sorted(self.context_methods),
            },
            "background_subagent_status_counts": self._background_subagent_status_counts(),
            "background_subagents": [
                self.background_subagents[key]
                for key in sorted(self.background_subagents)
            ],
            "handoffs": [self.handoffs[key] for key in sorted(self.handoffs)],
            "skill_load_successes": self.skill_load_successes,
            "skill_load_failures": self.skill_load_failures,
            "skills_used": sorted(self.skill_names),
            "final_status": self.final_status,
            "errors": list(self.errors),
            "timeline_omitted": self.timeline_omitted,
        }

    def _background_subagent_status_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.background_subagents.values():
            status = str(task.get("status", "unknown"))
            counts[status] = counts.get(status, 0) + 1
        return dict(sorted(counts.items()))

    def elapsed_ms(self) -> int:
        return round((time.perf_counter() - self.started_at) * 1000)

    def _timeline(self, text: str) -> None:
        if len(self.timeline_head) < TIMELINE_HEAD_ITEMS:
            self.timeline_head.append(text)
            return
        if len(self.timeline_tail) >= TIMELINE_TAIL_ITEMS:
            self.timeline_tail.pop(0)
            self.timeline_omitted += 1
        self.timeline_tail.append(text)

    def timeline_items(self) -> list[str]:
        if self.timeline_omitted <= 0:
            return self.timeline_head + self.timeline_tail
        return (
            self.timeline_head
            + [f"... omitted {self.timeline_omitted} event(s) ..."]
            + self.timeline_tail
        )


def trace_summary_event(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "trace_summary",
        **summary,
    }


def compact_payload(value: Any, *, limit: int = MAX_PAYLOAD_TEXT) -> Any:
    safe = json_safe(value)
    return _trim_nested(safe, limit=limit)


def json_safe(value: Any) -> Any:
    if isinstance(value, BaseMessage):
        return message_to_dict(value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return json_safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    try:
        json.dumps(value)
        return value
    except TypeError:
        return repr(value)


def format_timeline_line(line: dict[str, Any]) -> str:
    event_type = str(line.get("type", "event"))
    payload = line.get("payload") if isinstance(line.get("payload"), dict) else {}
    if event_type == "graph_update":
        nodes = ", ".join(payload.get("nodes", []))
        return f"{line.get('elapsed_ms')}ms graph_update nodes={nodes}"
    if event_type.startswith("custom:"):
        event_name = payload.get("event_type", event_type.removeprefix("custom:"))
        node = payload.get("node", "")
        name = payload.get("name", "")
        suffix = " ".join(part for part in [str(node), str(name)] if part)
        return f"{line.get('elapsed_ms')}ms {event_name} {suffix}".rstrip()
    return f"{line.get('elapsed_ms')}ms {event_type}"


def build_timeline_markdown(summary: dict[str, Any], timeline: list[str]) -> str:
    lines = [
        "# MokioClaw Trace Timeline",
        "",
        f"- trace_id: {summary.get('trace_id', '')}",
        f"- status: {summary.get('status', '')}",
        f"- duration_ms: {summary.get('duration_ms', 0)}",
        f"- workspace: {summary.get('workspace', '')}",
        f"- events: {summary.get('event_count', 0)}",
        "",
        "## Summary",
        "",
        f"- nodes: {summary.get('node_visits', {})}",
        f"- tool_calls: {summary.get('tool_calls', 0)}",
        f"- failed_tool_calls: {summary.get('failed_tool_calls', 0)}",
        f"- approvals: {summary.get('approval_count', 0)}",
        f"- checkpoints: {summary.get('checkpoint_count', 0)}",
        f"- final_status: {summary.get('final_status', '')}",
        "",
        "## Timeline",
        "",
    ]
    lines.extend(f"- {item}" for item in timeline)
    if not timeline:
        lines.append("- (none)")
    return "\n".join(lines).rstrip() + "\n"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    tmp.replace(path)


def normalize_trace_path(path: Path) -> Path:
    return path.resolve()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _trim_nested(value: Any, *, limit: int) -> Any:
    if isinstance(value, str):
        return value if len(value) <= limit else value[: limit - 3] + "..."
    if isinstance(value, dict):
        return {key: _trim_nested(item, limit=limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_trim_nested(item, limit=limit) for item in value[:80]]
    return value


def _new_trace_id() -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"trace-{stamp}-{uuid4().hex[:6]}"
