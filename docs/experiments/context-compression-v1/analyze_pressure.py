"""Read-only post-hoc context exposure; does not change frozen scores or run models."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from mokioclaw.evaluation.overhead import _event, read_events


def analyze(root: Path) -> dict[str, Any]:
    report = json.loads((root / "report.json").read_text())
    rows = []
    for trial in report["results"]:
        archived = root / trial["arm"] / "trials" / trial["trial_id"] / "trace.tar.gz"
        live = list(
            (Path(trial["receipt"]).parent / "workspace/.mokioclaw/traces").glob("*/events.jsonl")
        )
        trace = archived if archived.exists() else live[0]
        events = [_event(r) for r in read_events(trace)]
        budgets = [e for e in events if e.get("type") == "context_budget"]
        monitors = [e for e in events if e.get("type") == "context_monitor"]
        rows.append(
            {
                "arm": trial["arm"],
                "task_id": trial["task_id"],
                "repeat": trial["repeat"],
                "run_id": trial["run_id"],
                "compression_applied": trial["compression_applied"],
                "summary_requests": trial["compression_requests"],
                "max_request_input_estimate": max(
                    (e["input_tokens"] for e in budgets), default=None
                ),
                "high_watermarks": sorted({e["high_watermark"] for e in budgets}),
                "input_limits": sorted({e["token_limit"] for e in budgets}),
                "graph_watermark_crossings": sum(
                    e.get("token_count", 0) >= e.get("token_limit", 1) for e in monitors
                ),
                "source_trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "kind": "post_hoc_context_exposure",
        "frozen_runtime_commit": report["harness_revision"],
        "recorded_trials": len(rows),
        "original_scores_unchanged": True,
        "rows": rows,
        "notes": [
            "Input estimates and watermarks are runtime trigger diagnostics, not API-reported usage.",
            "No causal token/latency benefit is attributable to compression in trials without actual compression.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
