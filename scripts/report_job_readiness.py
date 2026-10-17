"""Read-only comparison from the shipped B0/B1 archives; never runs models or tools."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import tarfile
from collections import Counter
from pathlib import Path
from typing import Any

from mokioclaw.evaluation.models import EvalTrialResult
from mokioclaw.evaluation.overhead import analyze_overhead, read_events
from mokioclaw.evaluation.runner import write_json


def distribution(values: list[int | float]) -> dict[str, Any]:
    return {"n": len(values), "min": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "max": max(values) if values else None}


def claim_marker(text: str) -> bool | None:
    marker = re.search(r"(?:^|\n)\s*AGENT_CLAIM:\s*(solved|unsolved)\s*$", text.strip())
    return marker[1] == "solved" if marker else None


def checkpoint(trial: Path) -> dict[str, Any]:
    with tarfile.open(trial / "trace.tar.gz") as archive:
        try:
            handle = archive.extractfile(".mokioclaw/checkpoints/checkpoint.json")
        except KeyError:
            return {}
        if handle is None:
            return {}
        value: dict[str, Any] = json.loads(handle.read(1024 * 1024))
        summary = value.get("summary", {})
        return summary if isinstance(summary, dict) else {}


def observations(trial: Path, version: str) -> dict[str, Any]:
    result = EvalTrialResult.model_validate_json((trial / "result.json").read_text())
    records = read_events(trial / "trace.tar.gz")
    overhead = analyze_overhead(records)
    summary = checkpoint(trial)
    # Original worker.model_claim can be overwritten by verifier.passed. Keep it
    # as a legacy observation and independently parse the final report marker.
    explicit_claim = claim_marker(summary.get("final_answer", ""))
    role_claims = []
    with tarfile.open(trial / "trace.tar.gz") as archive:
        for record in records:
            event = record.get("payload", {}).get("payload", {})
            if (not isinstance(event, dict) or event.get("type") != "llm_stream_end"
                    or event.get("status") != "finished"):
                continue
            text = event.get("text", "")
            reference = event.get("full_output_path")
            if reference:
                try:
                    handle = archive.extractfile(reference)
                except KeyError:
                    handle = None
                if handle is not None:
                    text = handle.read(1024 * 1024).decode(errors="replace")
            claimed = claim_marker(text)
            if claimed is not None:
                role_claims.append({"role": event.get("role") or event.get("node"),
                                    "stream_id": event.get("stream_id"),
                                    "claim": claimed, "reference": reference,
                                    "output_truncated": event.get("output_truncated", False)})
    actor_claims = [c["claim"] for c in role_claims if c["role"] == "codeAgent"]
    verified = summary.get("passed") if "verifier" in overhead["roles"] else None
    metrics = result.model_metrics
    changed = (json.loads((trial / "file-changes.json").read_text())
               if (trial / "file-changes.json").exists() else {})
    source_paths = [name for name in changed if name.startswith("src/")]
    issues = []
    canonical_usage = {k: sum(r[k] for r in overhead["roles"].values())
                       for k in ("input_tokens", "output_tokens", "total_tokens")}
    for key, value in canonical_usage.items():
        if value != metrics.get(key, 0):
            issues.append(f"{key}: canonical {value} != receipt {metrics.get(key)}")
    run_ids = {r.get("run_id") for r in records if r.get("run_id")}
    if run_ids != {result.run_id}:
        issues.append("Canonical run IDs do not match receipt")
    requests = []
    estimated_usage_requests = 0
    tool_ids = set()
    for record in records:
        event = record.get("payload", {}).get("payload", {})
        if not isinstance(event, dict):
            continue
        if event.get("type") == "llm_request":
            requests.append(event["stream_id"])
        if event.get("type") == "model_usage" and event.get("estimated"):
            estimated_usage_requests += 1
        if event.get("tool_call_id"):
            tool_ids.add(event["tool_call_id"])
    if set(requests) != set(result.stream_ids):
        issues.append("Receipt stream IDs differ from canonical requests")
    if not set(result.tool_call_ids).issubset(tool_ids):
        issues.append("Receipt tool IDs missing from canonical trace")
    row: dict[str, Any] = {
        "version": version, "task_id": result.config.task_id, "repeat": result.config.repeat,
        "trial_id": result.trial_id, "experiment_id": result.experiment_id,
        "execution_status": result.execution_status, "grade_valid": result.grade.valid,
        "task_solved": result.task_solved, "independent_artifact_passed": result.grade.solved,
        "partial_artifact_passed": result.grade.solved and not result.task_solved,
        "functional_tests_passed": (result.grade.valid and result.grade.tests_run > 0
                                     and result.grade.target_passed > 0
                                     and not result.grade.target_failed
                                     and not result.grade.regression_failed),
        "source_files_changed": source_paths,
        "multiple_source_file_delivery": result.task_solved and len(source_paths) >= 2,
        "explicit_final_model_claim": explicit_claim, "verifier_passed": verified,
        "explicit_code_agent_claim": actor_claims[-1] if actor_claims else None,
        "explicit_role_claims": role_claims,
        "code_agent_claim_matches_external_grade": (actor_claims[-1] == result.grade.solved)
        if actor_claims and result.grade.valid else None,
        "legacy_worker_claim": result.model_claim,
        "explicit_claim_matches_external_grade": (explicit_claim == result.grade.solved)
        if explicit_claim is not None and result.grade.valid else None,
        "verifier_matches_external_grade": (verified == result.grade.solved)
        if verified is not None and result.grade.valid else None,
        "stop_reason": result.stop_reason, "infrastructure_error": result.infrastructure_error,
        "grade": result.grade.model_dump(), "safety_assertions": result.safety_assertions,
        "requests": metrics.get("requests", 0), "tools": result.tool_count,
        "model_metrics": metrics, "generation_seconds": result.generation_seconds,
        "estimated_usage_requests": estimated_usage_requests,
        "grading_seconds": result.grade.seconds, "actual_path": result.actual_path,
        "run_id": result.run_id, "stream_ids": result.stream_ids,
        "tool_call_ids": result.tool_call_ids, "role_overhead": overhead,
        "additional_audit_issues": issues,
        "archive": str(trial), "final_answer": summary.get("final_answer"),
        "verifier_summary": summary.get("verifier_summary"),
    }
    write_json(trial / "role-overhead.json", overhead)
    return row


def summarize(rows: list[dict[str, Any]], denominator: int) -> dict[str, Any]:
    metrics = [r["model_metrics"] for r in rows]
    missing_usage = sum(s["requests_without_usage"] for r in rows
                        for s in r["role_overhead"]["roles"].values())
    known_usage: dict[str, Any] = {}
    for name in ("input_tokens", "output_tokens", "total_tokens", "cache_read_tokens",
                 "cache_creation_tokens"):
        values = [m[name] for m in metrics if m.get(name) is not None]
        known_usage[name] = {"known_sum": sum(values) if values else None,
                             "known_trials": len(values), "unknown_trials": len(rows) - len(values),
                             "incomplete": bool(missing_usage) or len(values) != len(rows) or (
                                 name.startswith("cache_") and any(
                                     m.get("cache_usage_incomplete") for m in metrics))}
    roles: dict[str, Any] = {}
    for row in rows:
        for role, counts in row["role_overhead"]["roles"].items():
            total = roles.setdefault(role, {"requests": 0, "input_tokens": 0,
                                           "output_tokens": 0, "tool_results": 0})
            for key in total:
                total[key] += counts[key]
    return {
        "scheduled_denominator": denominator, "terminal": len(rows),
        "complete_deliveries": sum(r["task_solved"] for r in rows),
        "independent_artifact_passes": sum(r["independent_artifact_passed"] for r in rows),
        "partial_artifact_passes": sum(r["partial_artifact_passed"] for r in rows),
        "functional_test_passes": sum(r["functional_tests_passed"] for r in rows),
        "multiple_source_file_deliveries": sum(r["multiple_source_file_delivery"] for r in rows),
        "invalid_grades": sum(not r["grade_valid"] for r in rows),
        "infrastructure_failures": sum(bool(r["infrastructure_error"])
                                       or r["execution_status"] == "infrastructure_failed" for r in rows),
        "regression_failures": sum(bool(r["grade"]["regression_failed"]) for r in rows),
        "out_of_scope_trials": sum(bool(r["grade"]["out_of_scope"]) for r in rows),
        "tampered_test_trials": sum(bool(r["grade"]["tampered_tests"]) for r in rows),
        "stop_reasons": dict(Counter(r["stop_reason"] or (
            "finished" if r["execution_status"] == "finished" else "unspecified_" + r["execution_status"]
        ) for r in rows)),
        "verifier_entered_trials": sum("verifier" in r["role_overhead"]["roles"] for r in rows),
        "completion_reserve_triggered_trials": sum(bool(r["role_overhead"]["completion_budget_events"])
                                                   for r in rows),
        "last_requested_roles": dict(Counter(r["role_overhead"]["last_requested_role"] for r in rows)),
        "assessments": {field: {"true": sum(r[field] is True for r in rows),
                                 "false": sum(r[field] is False for r in rows),
                                 "unknown": sum(r[field] is None for r in rows)}
                        for field in ("explicit_code_agent_claim", "verifier_passed",
                                      "code_agent_claim_matches_external_grade",
                                      "verifier_matches_external_grade")},
        "requests_total": sum(r["requests"] for r in rows),
        "tools_total": sum(r["tools"] for r in rows), "usage": known_usage,
        "usage_estimated_trials": sum(bool(m.get("estimated")) for m in metrics),
        "estimated_usage_requests": sum(r["estimated_usage_requests"] for r in rows),
        "requests_without_usage": missing_usage,
        "distributions": {key: distribution([r[key] for r in rows]) for key in (
            "requests", "tools", "generation_seconds", "grading_seconds")},
        "complete_delivery_seconds": distribution([r["generation_seconds"] for r in rows
                                                    if r["task_solved"]]),
        "non_delivery_seconds": distribution([r["generation_seconds"] for r in rows
                                               if not r["task_solved"]]),
        "roles": roles,
    }


def report(root: Path, plan_path: Path) -> dict[str, Any]:
    plan = json.loads(plan_path.read_text())
    rows = []
    for item in plan["trials"]:
        version = item["version"]
        name = f"{item['task_id']}-{item['architecture']}-{item['repeat']}"
        trial = root / version.lower() / "trials" / name
        if not (trial / "result.json").exists():
            continue
        row = observations(trial, version)
        if row["execution_status"] in {"running", "not_started"}:
            raise ValueError("Report cannot count a nonterminal receipt")
        rows.append(row)
    versions = {v: summarize([r for r in rows if r["version"] == v], 9) for v in ("B0", "B1")}
    tasks = {task: {v: summarize([r for r in rows if r["version"] == v and r["task_id"] == task], 3)
                    for v in ("B0", "B1")} for task in ("M02-v2", "M07-v2", "M08-v2")}
    return {"schema_version": 1, "real_model": True, "scheduled": 18, "terminal": len(rows),
            "complete_matrix": len(rows) == 18, "versions": versions, "tasks": tasks, "trials": rows,
            "limitations": ["18 fixed trials, 3 repetitions per version/task, no broad generalization",
                            "Historical 0/56 remains unchanged; no official SWE-bench score",
                            "Cache reads included in input; unknown creation usage is not zero",
                            "Runtime worker claim may conflate verifier; parsed claims reported separately",
                            "Token budget uses estimates/reservation, not a precise billing hard limit",
                            "Failure and delivery durations have separate distributions"]}


def markdown(value: dict[str, Any]) -> str:
    lines = ["# Goal 2：真实固定任务对照结果", "",
             f"已归档终态：{value['terminal']}/18。完整矩阵：{value['complete_matrix']}。", "",
             "完整交付要求运行正常结束且独立可信测试通过；失败后的产物通过另列。", "",
             "| 版本 | 完整交付 / 固定分母 | 产物通过（含部分） | 请求 | 工具 | 已报告 token | 基础设施失败 |",
             "|---|---|---|---|---|---|---|"]
    for version, row in value["versions"].items():
        lines.append(f"| {version} | {row['complete_deliveries']}/9 | "
                     f"{row['independent_artifact_passes']} | {row['requests_total']} | "
                     f"{row['tools_total']} | {row['usage']['total_tokens']['known_sum']} | "
                     f"{row['infrastructure_failures']} |")
    lines += ["", "| 顺序 | 版本 | 任务 / 重复 | 运行终态 | 独立通过 | 完整交付 | 请求 | token | 停止原因 |",
              "|---|---|---|---|---|---|---|---|---|"]
    for index, row in enumerate(value["trials"], 1):
        lines.append(f"| {index} | {row['version']} | {row['task_id']} / {row['repeat']} | "
                     f"{row['execution_status']} | {row['independent_artifact_passed']} | "
                     f"{row['task_solved']} | {row['requests']} | "
                     f"{row['model_metrics'].get('total_tokens')} | {row['stop_reason'] or '—'} |")
    lines += ["", "每任务三次的 min/median/max、成功与失败耗时、角色开销、缓存未知量及验收明细见 comparison.json。",
              "显式模型自评按最终汇报标记解析；未出现时记未知。内部 verifier 与外部测试分别记录。",
              "预算停止保留文件产物，不能等同于完整交付。成功率提升不是本批完成的必要条件。", "",
              *["- " + s for s in value["limitations"]], ""]
    return "\n".join(lines)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--plan", type=Path, default=Path("docs/experiments/job-readiness-v1/goal2-input.json"))
    args = parser.parse_args()
    value = report(args.root, args.plan)
    write_json(args.root / "comparison.json", value)
    (args.root / "RESULTS.md").write_text(markdown(value))
    print(json.dumps({"terminal": value["terminal"], "complete_matrix": value["complete_matrix"]}))
