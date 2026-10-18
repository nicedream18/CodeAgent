"""Compression A/B measurements from canonical streams, never model self-scoring."""

from __future__ import annotations

import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from mokioclaw.evaluation.models import EvalTrialResult
from mokioclaw.evaluation.overhead import _event, read_events


def distribution(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "min": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "max": max(values) if values else None}


def optional_sum(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    known = [r[key] for r in rows if r.get(key) is not None]
    return {"known_sum": sum(known) if known else None,
            "unknown_requests": len(rows) - len(known), "incomplete": len(known) != len(rows)}


def measure_trial(directory: Path, enabled: bool) -> dict[str, Any]:
    import json

    result = EvalTrialResult.model_validate_json((directory / "result.json").read_text())
    worker = json.loads((directory / "worker.json").read_text()) if (directory / "worker.json").exists() else {}
    files = list((directory / "workspace/.mokioclaw/traces").glob("*/events.jsonl"))
    issues = []
    try:
        records = read_events(files[0]) if len(files) == 1 else []
    except (OSError, ValueError) as exc:
        records = []
        issues.append("Canonical trace unreadable: " + str(exc)[:200])
    if len(files) != 1:
        issues.append("Expected one canonical run trace")
    requests: dict[str, dict[str, Any]] = {}
    usage: dict[str, dict[str, Any]] = {}
    ends: Counter[str] = Counter()
    compressions: dict[str, dict[str, Any]] = {}
    policies: set[bool] = set()
    for record in records:
        event = _event(record)
        stream = str(event.get("stream_id") or "")
        kind = event.get("type")
        if kind == "llm_request":
            if stream in requests:
                issues.append("Duplicate request stream: " + stream)
            requests[stream] = event
        elif kind == "model_usage":
            if stream in usage:
                issues.append("Duplicate usage stream: " + stream)
            usage[stream] = event
        elif kind == "llm_stream_end":
            ends[stream] += 1
        elif kind == "context_compression":
            identity = str(event.get("event_id") or "")
            if not identity:
                issues.append("Compression event has no identity")
            compressions[identity] = event
        elif kind == "context_budget" and "compression_enabled" in event:
            policies.add(event["compression_enabled"])
    if requests.keys() != usage.keys() or any(ends[s] != 1 for s in requests):
        issues.append("Request/usage/end streams are incomplete or nonunique")
    if len(requests) != result.model_metrics.get("requests", 0):
        issues.append("Canonical request count differs from runtime receipt")
    expected_model = {"model": result.config.model, "context_tokens": result.config.context_tokens,
                      "output_tokens": result.config.output_tokens, "sdk_max_retries": 0}
    if any(r.get("model_config") != expected_model for r in requests.values()):
        issues.append("Actual request model/budget/retry configuration differs from frozen trial")
    if requests and (worker.get("context_compression_enabled") is not enabled or policies != {enabled}):
        issues.append("Actual runtime compression policy differs from assigned arm")
    compressor_ids = {s for s, r in requests.items() if r.get("node") == "context_compressor"}
    if not enabled and (compressions or compressor_ids):
        issues.append("Disabled arm performed compression")
    values = list(usage.values())
    totals = {key: sum(u.get(key, 0) for u in values)
              for key in ("input_tokens", "output_tokens", "total_tokens")}
    for key, value in totals.items():
        if value != result.model_metrics.get(key, 0):
            issues.append("Canonical usage differs from runtime receipt: " + key)
    if requests.keys() != usage.keys() or not records:
        totals = {key: result.model_metrics.get(key, 0) for key in totals}
    reported = [u for u in values if u.get("reported") and not u.get("estimated")]
    compression_usage = [usage[s] for s in compressor_ids if s in usage]
    failure_category = (
        "context_budget_error" if str(worker.get("error", "")).startswith("ContextBudgetError:")
        else "infrastructure_failure" if result.infrastructure_error
        else str(result.stop_reason or result.execution_status)
    )
    return {
        "trial_id": result.trial_id, "task_id": result.config.task_id,
        "repeat": result.config.repeat, "compression_enabled": enabled,
        "run_id": result.run_id, "execution_status": result.execution_status,
        "stop_reason": result.stop_reason, "worker_error": worker.get("error"),
        "failure_category": failure_category,
        "infrastructure_error": result.infrastructure_error,
        "task_solved": result.task_solved, "artifact_passed": result.grade.solved,
        "grade_valid": result.grade.valid, "out_of_scope": result.grade.out_of_scope,
        "tests_run": result.grade.tests_run, "model_claim": result.model_claim,
        "requests": len(requests), "tools": result.tool_count, **totals,
        "reported_total_tokens": sum(u["total_tokens"] for u in reported),
        "estimated_requests": sum(bool(u.get("estimated")) for u in values),
        "usage_missing_requests": max(0, result.model_metrics.get("requests", 0) - len(values)),
        "cache_read_tokens": optional_sum(values, "cache_read_tokens"),
        "cache_creation_tokens": optional_sum(values, "cache_creation_tokens"),
        "compression_requests": len(compressor_ids),
        "compression_tokens": sum(u["total_tokens"] for u in compression_usage),
        "compression_model_ms": sum(u["duration_ms"] for u in compression_usage),
        "compression_applied": sum(e.get("status") == "applied" for e in compressions.values()),
        "compression_events": list(compressions.values()),
        "generation_seconds": result.generation_seconds, "grading_seconds": result.grade.seconds,
        "actual_path": result.actual_path, "stream_ids": sorted(requests),
        "tool_call_ids": result.tool_call_ids, "receipt": str(directory / "result.json"),
        "measurement_valid": not issues, "measurement_issues": issues,
    }


def summarize(rows: list[dict[str, Any]], planned: int) -> dict[str, Any]:
    terminal = [r for r in rows if r["execution_status"] not in {"running", "not_started"}]
    solved = [r for r in terminal if r["task_solved"]]
    return {
        "planned": planned, "terminal": len(terminal), "solved": len(solved),
        "completion_rate": len(solved) / planned if planned and len(terminal) == planned else None,
        "artifact_passed": sum(r["artifact_passed"] for r in terminal),
        "invalid_grades": sum(not r["grade_valid"] for r in terminal),
        "infrastructure_failed": sum(bool(r["infrastructure_error"]) for r in terminal),
        "out_of_scope_trials": sum(bool(r["out_of_scope"]) for r in terminal),
        "input_tokens": sum(r["input_tokens"] for r in terminal),
        "output_tokens": sum(r["output_tokens"] for r in terminal),
        "total_tokens": sum(r["total_tokens"] for r in terminal),
        "provider_reported_total_tokens": sum(r["reported_total_tokens"] for r in terminal),
        "estimated_requests": sum(r["estimated_requests"] for r in terminal),
        "usage_missing_requests": sum(r["usage_missing_requests"] for r in terminal),
        "requests": sum(r["requests"] for r in terminal),
        "compression_requests": sum(r["compression_requests"] for r in terminal),
        "compression_tokens": sum(r["compression_tokens"] for r in terminal),
        "compression_applied": sum(r["compression_applied"] for r in terminal),
        "compression_model_ms": sum(r["compression_model_ms"] for r in terminal),
        "on_trials_without_applied_compression": sum(
            r["compression_enabled"] and not r["compression_applied"] for r in terminal
        ),
        "generation_seconds": distribution([r["generation_seconds"] for r in terminal]),
        "successful_generation_seconds": distribution([r["generation_seconds"] for r in solved]),
        "grading_seconds": distribution([r["grading_seconds"] for r in terminal]),
        "stop_reasons": dict(Counter(r["stop_reason"] or r["execution_status"] for r in terminal)),
        "failure_categories": dict(Counter(r["failure_category"] for r in terminal)),
        "cache_read_tokens": {
            "known_sum": sum(r["cache_read_tokens"]["known_sum"] or 0 for r in terminal)
            if any(r["cache_read_tokens"]["known_sum"] is not None for r in terminal) else None,
            "unknown_requests": sum(r["cache_read_tokens"]["unknown_requests"] for r in terminal)},
        "cache_creation_tokens": {
            "known_sum": sum(r["cache_creation_tokens"]["known_sum"] or 0 for r in terminal)
            if any(r["cache_creation_tokens"]["known_sum"] is not None for r in terminal) else None,
            "unknown_requests": sum(r["cache_creation_tokens"]["unknown_requests"] for r in terminal)},
        "measurement_valid": all(r["measurement_valid"] for r in terminal),
    }


def markdown(report: dict[str, Any]) -> str:
    lines = ["# Context Compression A/B Benchmark", "",
             f"实验：`{report['experiment_id']}`；冻结提交：`{report['harness_revision']}`。",
             "", "| 压缩 | 完整交付 / 固定分母 | 产物通过 | 总 token（含明确估算） | 摘要 token（已计入总量） | 请求 | 生成秒 min / median / max |",
             "|---|---|---|---|---|---|---|"]
    for arm, row in report["arms"].items():
        d = row["generation_seconds"]
        lines.append(f"| {arm} | {row['solved']}/{row['planned']} | {row['artifact_passed']} | "
                     f"{row['total_tokens']} | {row['compression_tokens']} | {row['requests']} | "
                     f"{d['min']} / {d['median']} / {d['max']} |")
    lines += ["", "完整交付 = 运行 finished 且可信独立评分通过；产物通过另列。失败、预算停止和基础设施问题保留在固定分母内。",
              "摘要请求也消耗同一运行预算，摘要 token 不再额外相加。缓存读取包含在输入中；未知缓存不写零。",
              "延迟包括失败停止，不可直接解释为完成任务更快；成功耗时分布另列 JSON。",
              "", f"计划完整：{report['complete']}；计量核对有效：{report['measurement_valid']}。",
              "", "每任务三次分布、配对差值、估算标记、缓存未知量和调用 ID 见 report.json。",
              "相同 temperature 不保证远端确定性；小样本不作显著性或广泛泛化结论。",
              "关闭压缩仍保留工具结果裁剪、输入硬限制、取消和权限；上下文超限会安全停止。",
              "开启但未实际压缩的 trial 单列，不能作为压缩收益证据。", ""]
    return "\n".join(lines)
