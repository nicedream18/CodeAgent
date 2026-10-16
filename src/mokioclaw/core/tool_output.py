"""Bound structured tool results without cutting serialized JSON in half."""

from __future__ import annotations

import hashlib
import json
import os
from itertools import chain
from pathlib import Path
from typing import Any
from uuid import uuid4

PRIORITY = (
    "ok",
    "error_type",
    "tool_name",
    "subagent_run_id",
    "parent_run_id",
    "handoff_id",
    "status",
    "path",
    "stdout_path",
    "stderr_path",
    "complete",
    "offset",
    "limit",
    "total_lines",
    "error",
)
OUTPUT_METADATA = frozenset(
    {
        "output_truncated",
        "omitted_characters",
        "omitted_items",
        "truncation_reasons",
        "full_output_path",
        "artifact_error",
        "model_visible_complete",
        "continue_reading",
    }
)


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def bound_value(
    value: Any,
    *,
    field_limit: int = 12000,
    total_limit: int = 24000,
    max_items: int = 100,
    max_depth: int = 8,
    max_nodes: int = 1000,
) -> Any:
    if min(field_limit, max_items, max_depth, max_nodes) <= 0 or total_limit < 1024:
        raise ValueError(
            "tool output limits must be positive; total limit must be at least 1024 characters"
        )

    verified_nodes = 0

    def already_bounded(item, depth=0, metadata=False):
        nonlocal verified_nodes
        verified_nodes += 1
        if depth > max_depth or verified_nodes > max_nodes:
            return False
        if isinstance(item, str):
            return len(item) <= (512 if metadata else field_limit + 64)
        if isinstance(item, dict):
            content_count = sum(
                1 for key in item if depth != 0 or key not in OUTPUT_METADATA
            )
            return content_count <= max_items and all(
                already_bounded(v, depth + 1, depth == 0 and key in OUTPUT_METADATA)
                for key, v in item.items()
            )
        if isinstance(item, (list, tuple)):
            return len(item) <= max_items and all(
                already_bounded(v, depth + 1) for v in item
            )
        return True

    if isinstance(value, dict) and value.get("output_truncated"):
        try:
            if len(encoded(value)) <= total_limit and already_bounded(value):
                return value
        except (ValueError, RecursionError):
            value = dict(
                value
            )  # Circular/unserializable inputs use the bounded visitor.
    remaining = total_limit - 768
    nodes = 0
    omitted = 0
    omitted_items = 0
    reasons: set[str] = set()
    seen: set[int] = set()

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining, nodes, omitted, omitted_items
        nodes += 1
        if nodes > max_nodes or depth > max_depth or remaining < 32:
            reasons.add("structure_budget")
            return "[omitted]"
        if isinstance(item, str):
            available = max(0, min(field_limit, (remaining - 64) // 2))
            if len(item) > available:
                omitted += len(item) - available
                reasons.add(
                    "field_limit" if len(item) > field_limit else "result_limit"
                )
                item = (
                    item[:available]
                    + f"\n… [{len(item) - available} characters omitted]"
                )
            # JSON escaping can require up to six characters per character.
            while len(encoded(item)) > remaining and len(item) > 16:
                item = item[: len(item) // 2] + "… [omitted]"
                reasons.add("result_limit")
            remaining -= len(encoded(item))
            return item
        if isinstance(item, (dict, list, tuple)):
            if id(item) in seen:
                reasons.add("cycle")
                return "[circular reference]"
            seen.add(id(item))
            remaining -= 2
            if isinstance(item, dict):
                result = {}
                keys = chain(
                    (key for key in PRIORITY if key in item),
                    (key for key in item if key not in PRIORITY),
                )
                for index, key in enumerate(keys):
                    if index >= max_items or nodes >= max_nodes or remaining < 64:
                        omitted_items += len(item) - index
                        reasons.add("collection_limit")
                        break
                    bounded_key = str(key)[:128]
                    remaining -= len(encoded(bounded_key)) + 2
                    result[bounded_key] = visit(item[key], depth + 1)
            else:
                result = []
                for index, child in enumerate(item):
                    if index >= max_items or nodes >= max_nodes or remaining < 64:
                        omitted_items += len(item) - index
                        reasons.add("collection_limit")
                        break
                    remaining -= 1
                    result.append(visit(child, depth + 1))
            seen.remove(id(item))
            return result
        if not isinstance(item, (int, float, bool, type(None))):
            return visit(str(item), depth + 1)
        remaining -= len(encoded(item))
        return item

    result = visit(value)
    if not reasons:
        return result
    if not isinstance(result, dict):
        result = {"value": result}
    result.update(
        output_truncated=True,
        omitted_characters=omitted,
        omitted_items=omitted_items,
        truncation_reasons=sorted(reasons),
    )
    if len(encoded(result)) > total_limit:
        # Pathological escaping/keys still receive a valid, bounded envelope.
        result = {key: result[key] for key in PRIORITY if key in result}
        result.update(output_truncated=True, truncation_reasons=["result_limit"])
        if len(encoded(result)) > total_limit:
            result = {
                "ok": bool(result.get("ok")),
                "output_truncated": True,
                "error_type": "result_limit",
            }
    return result


def _store_result(workspace: Path, value: Any) -> str:
    root = workspace / ".mokioclaw" / "tool-results"
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / (uuid4().hex + ".tmp")
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as handle:
            for chunk in json.JSONEncoder(ensure_ascii=False, default=str).iterencode(
                value
            ):
                data = chunk.encode("utf-8")
                size += len(data)
                if size > 16 * 1024 * 1024:
                    raise ValueError("full result exceeds the 16 MiB artifact limit")
                digest.update(data)
                handle.write(data)
        target = root / (digest.hexdigest()[:24] + ".json")
        os.replace(temporary, target)
        artifacts = []
        for old in root.glob("*.json"):
            try:
                artifacts.append((old.stat().st_mtime_ns, old))
            except FileNotFoundError:
                continue
        for _, old in sorted(artifacts, reverse=True)[32:]:
            if old != target:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    continue
        return target.relative_to(workspace).as_posix()
    finally:
        temporary.unlink(missing_ok=True)


def bound_tool_result(
    value: Any,
    runtime: Any,
    *,
    tool_name: str = "",
    tool_call_id: str = "",
    writer=None,
) -> Any:
    result = bound_value(
        value,
        field_limit=runtime.context_output_limit_chars,
        total_limit=runtime.context_result_limit_chars,
        max_items=runtime.context_max_items,
        max_depth=runtime.context_max_depth,
        max_nodes=runtime.context_max_nodes,
    )
    if not isinstance(result, dict) or not result.get("output_truncated"):
        return result
    if isinstance(value, dict) and value.get("output_truncated"):
        return result
    try:
        result["full_output_path"] = _store_result(runtime.workspace, value)
    except (OSError, ValueError, TypeError, RecursionError) as exc:
        result["artifact_error"] = str(exc)[:160]
    if tool_name == "FileReadTool" and isinstance(value, dict):
        result["complete"] = False
        result["model_visible_complete"] = False
        path = value.get("path")
        if path:
            file_path = runtime.assert_workspace_path(runtime.workspace / path)
            if file_path.exists():
                runtime.record_read(file_path, complete=False)
        result["continue_reading"] = (
            "Use FileReadTool on the original path with offset/limit for lines or char_offset/char_limit for long single lines; full_output_path supports character ranges too."
        )
    # Additional metadata has a bounded reservation in bound_value.
    if len(encoded(result)) > runtime.context_result_limit_chars:
        result = {
            key: result[key]
            for key in (*PRIORITY, "full_output_path", "output_truncated")
            if key in result
        }
    if writer:
        writer(
            {
                "type": "tool_output_truncated",
                "event_id": uuid4().hex,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "retained_characters": len(encoded(result)),
                "omitted_characters": result.get("omitted_characters"),
                "omitted_items": result.get("omitted_items"),
                "reasons": result.get("truncation_reasons", []),
                "full_output_path": result.get("full_output_path"),
            }
        )
    return result
