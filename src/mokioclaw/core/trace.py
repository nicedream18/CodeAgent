from __future__ import annotations

import json
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import BaseMessage, message_to_dict

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


class TraceRecorder:
    def __init__(self, runtime: Any, task: str = "") -> None:
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
        self.final_status = ""
        self.timeline_head: list[str] = []
        self.timeline_tail: list[str] = []
        self.timeline_omitted = 0
        self.stream_buffers: dict[str, list[str]] = {}
        if self.enabled:
            setattr(runtime, "trace_id", self.trace_id)
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

    def record_custom_event(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        event_type = str(event.get("type", "custom_event"))
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
        if event_type == "llm_stream_start":
            self.stream_buffers[str(event.get("node", "model"))] = []
        elif event_type == "llm_token":
            self.stream_buffers.setdefault(str(event.get("node", "model")), []).append(
                str(event.get("text", ""))
            )
            return
        elif event_type == "llm_stream_end":
            node = str(event.get("node", "model"))
            event = {**event, "text": "".join(self.stream_buffers.pop(node, []))}
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

    def end(
        self,
        *,
        status: str,
        latest_node: str = "",
        final_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
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
            "skill_load_successes": self.skill_load_successes,
            "skill_load_failures": self.skill_load_failures,
            "skills_used": sorted(self.skill_names),
            "final_status": self.final_status,
            "errors": list(self.errors),
            "timeline_omitted": self.timeline_omitted,
        }

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
    return datetime.now(timezone.utc).isoformat()


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
