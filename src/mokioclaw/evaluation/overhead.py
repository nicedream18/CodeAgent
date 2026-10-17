"""Read-only role diagnostics from canonical traces, including imperfect runs."""

from __future__ import annotations

import hashlib
import json
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

MAX_TRACE_BYTES = 16 * 1024 * 1024
READ_TOOLS = {"FileReadTool", "GrepTool", "FileGlobTool"}


def read_events(path: Path) -> list[dict[str, Any]]:
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path) as archive:
            members = [
                m for m in archive
                if m.isfile() and m.name.startswith(".mokioclaw/traces/")
                and m.name.endswith("/events.jsonl")
            ]
            if len(members) != 1:
                raise ValueError("Expected one canonical run trace")
            handle = archive.extractfile(members[0])
            if handle is None:
                raise ValueError("Missing canonical trace")
            payload = handle.read(MAX_TRACE_BYTES + 1)
    else:
        with path.open("rb") as handle:
            payload = handle.read(MAX_TRACE_BYTES + 1)
    if len(payload) > MAX_TRACE_BYTES:
        raise ValueError("Canonical trace exceeds diagnostic input limit")
    return [json.loads(line) for line in payload.decode().splitlines() if line.strip()]


def _event(record: dict[str, Any]) -> dict[str, Any]:
    if str(record.get("type", "")).startswith("custom:"):
        payload = record.get("payload", {}).get("payload", {})
        return payload if isinstance(payload, dict) else {}
    payload = record.get("event", record)
    return payload if isinstance(payload, dict) else {}


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def analyze_overhead(records: list[dict[str, Any]]) -> dict[str, Any]:
    roles: dict[str, dict[str, Any]] = {}
    requests: dict[str, str] = {}
    usages: set[str] = set()
    seen_events: set[str] = set()
    ends: Counter[str] = Counter()
    results: set[tuple[str, str]] = set()
    read_fingerprints: Counter[str] = Counter()
    handoff_fingerprints: Counter[str] = Counter()
    pending: dict[tuple[str, str], list[dict[str, Any]]] = {}
    repeats: list[dict[str, Any]] = []
    last_role = "unknown"
    status: str | None = None
    reason: str | None = None
    read_without_args = 0
    budget_events = []
    latest_node = None

    def row(role: str) -> dict[str, Any]:
        if role not in roles:
            roles[role] = {
                "requests": 0, "usage_records": 0, "input_tokens": 0, "output_tokens": 0,
                "total_tokens": 0, "duration_ms": 0.0, "estimated": False,
                "cache_read_tokens": None, "cache_creation_tokens": None,
                "cache_usage_incomplete": False, "tool_results": 0, "tool_errors": 0,
                "invalid_input": 0, "unknown_tool": 0, "not_executed": 0,
            }
        return roles[role]

    for record in records:
        if record.get("type") == "run_end":
            final = record.get("payload", {})
            status = final.get("status")
            latest_node = final.get("latest_node")
        event = _event(record)
        kind = event.get("type")
        stream = str(event.get("stream_id") or "")
        if kind == "llm_stream_end":
            ends[stream] += 1
        identity = str(event.get("event_id") or "")
        if identity and identity in seen_events:
            continue
        if identity:
            seen_events.add(identity)
        role = str(event.get("agent") or event.get("role") or event.get("subagent")
                   or event.get("node") or "unknown")
        if kind == "llm_request":
            if stream in requests:
                continue
            requests[stream] = role
            row(role)["requests"] += 1
            last_role = role
        elif kind == "model_usage":
            if stream and stream in usages:
                continue
            if stream:
                usages.add(stream)
            stats = row(role)
            stats["usage_records"] += 1
            stats["estimated"] |= bool(event.get("estimated", False))
            for key in ("input_tokens", "output_tokens", "total_tokens", "duration_ms"):
                stats[key] += event.get(key, 0)
            for key in ("cache_read_tokens", "cache_creation_tokens"):
                if event.get(key) is None:
                    stats["cache_usage_incomplete"] = True
                else:
                    stats[key] = (stats[key] or 0) + event[key]
        elif kind == "tool_call":
            pending.setdefault((role, str(event.get("name"))), []).append(event.get("args", {}))
        elif kind == "tool_result":
            call = str(event.get("tool_call_id") or "")
            if call and (role, call) in results:
                continue
            if call:
                results.add((role, call))
            value = event.get("result", {})
            if not isinstance(value, dict):
                continue
            stats = row(role)
            stats["tool_results"] += 1
            stats["tool_errors"] += value.get("ok") is False
            stats["invalid_input"] += value.get("error_type") == "invalid_input"
            stats["unknown_tool"] += "unknown tool:" in str(value.get("error", ""))
            stats["not_executed"] += value.get("status") == "not_executed"
            name = str(event.get("name"))
            queue = pending.get((role, name), [])
            args = queue.pop(0) if queue else None
            if name in READ_TOOLS and value.get("ok"):
                if args is None:
                    read_without_args += 1
                    continue
                # A heuristic, not a cache hit or proof of wasted work. Changed
                # output/context invalidates it; timing and backend do not.
                observed = {k: v for k, v in value.items() if k not in {
                    "elapsed_ms", "duration_ms", "backend", "full_output_path",
                }}
                fingerprint = _hash([role, name, args, observed, event.get("context_version", 0)])
                read_fingerprints[fingerprint] += 1
                if read_fingerprints[fingerprint] > 1:
                    repeats.append({"role": role, "tool": name, "tool_call_id": call,
                                    "fingerprint": fingerprint})
        elif kind == "handoff":
            handoff_fingerprints[_hash([event.get("to"), event.get("instruction"),
                                        event.get("context_version", 0)])] += 1
        elif kind == "completion_budget":
            budget_events.append(event)
        elif kind in {"run_finished", "run_interrupted", "run_failed"}:
            status, reason = event.get("status"), event.get("stop_reason")
        elif kind == "run_budget_exhausted":
            reason = event.get("stop_reason")

    for role, stats in roles.items():
        stats["requests_without_usage"] = stats["requests"] - sum(
            stream in usages for stream, owner in requests.items() if owner == role
        )
    return {
        "schema_version": 1, "roles": roles, "requests": len(requests),
        "last_requested_role": last_role, "execution_status": status, "stop_reason": reason,
        "latest_node": latest_node,
        "duplicate_stream_end_observations": sum(max(0, n - 1) for n in ends.values()),
        "repeated_read_observations": repeats,
        "read_results_without_call_arguments": read_without_args,
        "repeated_handoff_observations": sum(max(0, n - 1) for n in handoff_fingerprints.values()),
        "completion_budget_events": budget_events,
        "definitions": {
            "tools": "Per-role tool_result observations include failed/denied/unexecuted calls; not an exact execution counter.",
            "repeats": "Same role, arguments, visible output and context version; heuristic only. Missing arguments are unmeasured.",
            "cache": "Known cache fields are partial sums; null is unknown. Cache reads are included in input tokens.",
            "duration": "Model usage duration only; excludes tool and end-to-end time.",
            "versions": "Canonical summaries can truncate bodies; use referenced source artifacts for deeper inspection.",
        },
    }
