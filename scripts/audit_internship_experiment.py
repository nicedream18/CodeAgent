"""Read-only audit of the frozen schedule and actual request configurations."""

from __future__ import annotations

import argparse
import json
import tarfile
from collections import Counter
from pathlib import Path

from mokioclaw.evaluation.models import EvalTrialResult
from mokioclaw.evaluation.prepare import cache_root


def trace_lines(trial: Path, member: str) -> list[str]:
    path = trial / member
    if path.is_file():
        with path.open("rb") as handle:
            payload = handle.read(16 * 1024 * 1024 + 1)
    else:
        with tarfile.open(trial / "trace.tar.gz") as archive:
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError("Missing trace member: " + member)
            payload = handle.read(16 * 1024 * 1024 + 1)
    if len(payload) > 16 * 1024 * 1024:
        raise ValueError("Trace input exceeds the bounded audit limit")
    return payload.decode().splitlines()


def audit(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    expected = {
        f"{c['task_id']}-{c['architecture']}-{c['repeat']}": c
        for item in manifest["trials"]
        for c in [item["config"]]
    }
    issues = []
    observability_issues = []
    request_roles = Counter()
    observed = Counter()
    completed = 0
    requests = 0
    trials = []
    paths = list(root.glob("trials/*/result.json"))
    for path in sorted(paths):
        result = EvalTrialResult.model_validate_json(path.read_text())
        name = result.trial_id
        if result.config.model_dump() != expected.get(name) or path.parent.name != name:
            issues.append(f"{name}: receipt does not match frozen schedule")
        if result.task_sha256 != manifest["tasks"].get(result.config.task_id):
            issues.append(f"{name}: task version differs from frozen manifest")
        if not result.real_model or result.execution_status in {"running", "not_started"}:
            issues.append(f"{name}: not a terminal real-model record")
        else:
            completed += 1
        starts, ends, calls = Counter(), Counter(), Counter()
        delivered = Counter()
        for line in trace_lines(path.parent, "events.jsonl"):
            event = json.loads(line).get("event", {})
            if isinstance(event, dict) and event.get("type") == "llm_request":
                delivered[event.get("stream_id")] += 1
        trace_names = [
            p.relative_to(path.parent).as_posix()
            for p in (path.parent / "workspace/.mokioclaw/traces").glob("*/events.jsonl")
        ]
        if not trace_names and (path.parent / "trace.tar.gz").exists():
            with tarfile.open(path.parent / "trace.tar.gz") as archive:
                trace_names = [
                    m.name for m in archive if m.isfile()
                    and m.name.startswith(".mokioclaw/traces/") and m.name.endswith("/events.jsonl")
                ]
        if len(trace_names) != 1:
            issues.append(f"{name}: expected a single canonical run trace")
        for line in trace_lines(path.parent, trace_names[0]) if len(trace_names) == 1 else []:
            event = json.loads(line).get("payload", {}).get("payload", {})
            if not isinstance(event, dict):
                continue
            kind, stream = event.get("type"), event.get("stream_id")
            if kind == "llm_stream_start":
                starts[stream] += 1
            elif kind == "llm_stream_end":
                ends[stream] += 1
            elif kind == "llm_request":
                calls[stream] += 1
                profile = event.get("model_config", {})
                wanted = {
                    "model": result.config.model,
                    "context_tokens": result.config.context_tokens,
                    "output_tokens": result.config.output_tokens,
                    "sdk_max_retries": 0,
                }
                if profile != wanted:
                    issues.append(f"{name}/{stream}: request configuration differs from contract")
                observed[json.dumps(profile, sort_keys=True)] += 1
                request_roles[event.get("role") or event.get("node")] += 1
        if starts != calls or any(v != 1 for v in calls.values()):
            issues.append(f"{name}: canonical start/request identifiers do not match one-to-one")
        gaps = sum((calls - delivered).values())
        duplicates = sum(v - 1 for v in ends.values())
        if starts != ends or calls != delivered:
            observability_issues.append({
                "trial_id": name,
                "requests_missing_from_outer_events": gaps,
                "duplicate_stream_end_records": duplicates,
                "missing_end_ids": sorted(starts.keys() - ends.keys()),
            })
        if len(calls) != result.model_metrics.get("requests", 0):
            issues.append(f"{name}: request count differs from model metrics")
        if len(calls) > result.config.request_limit or result.tool_count > result.config.tool_limit:
            issues.append(f"{name}: global request/tool count exceeded contract")
        if not result.grade.valid:
            issues.append(f"{name}: independent grade invalid")
        requests += len(calls)
        trials.append({"trial_id": name, "requests_audited": len(calls)})
    if completed != len(expected):
        issues.append(f"Only {completed}/{len(expected)} terminal trials")
    return {
        "schema_version": 1,
        "experiment_id": json.loads((root / "archive-info.json").read_text())["experiment_id"]
        if (root / "archive-info.json").exists() else root.name,
        "frozen_revision": manifest["harness_revision"],
        "scheduled": len(expected),
        "terminal": completed,
        "request_configurations_audited": requests,
        "roles": dict(request_roles),
        "profiles": [{"config": json.loads(p), "requests": n} for p, n in observed.items()],
        "issues": issues,
        "comparison_conditions_passed": not issues,
        "observability_issues": observability_issues,
        "event_delivery_checks_passed": not observability_issues,
        "trials": trials,
        "note": "Configuration/counts use the canonical producer trace; outer delivery and duplicate end defects are reported separately, never erased. This is not an independent HTTP packet capture or a success score.",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("experiment_id")
    parser.add_argument("--cache", type=Path, default=cache_root())
    parser.add_argument("--archive", type=Path, help="Audit the shipped archive without a model cache")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.experiment_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in args.experiment_id
    ):
        raise SystemExit("Invalid experiment ID")
    record = audit(args.archive or args.cache / "experiments" / args.experiment_id)
    if record["experiment_id"] != args.experiment_id:
        raise SystemExit("Archive experiment ID does not match the requested experiment")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: v for k, v in record.items() if k != "trials"}, ensure_ascii=False))
    raise SystemExit(0 if record["comparison_conditions_passed"] else 1)
