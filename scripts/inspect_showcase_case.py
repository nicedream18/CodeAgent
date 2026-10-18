"""Read two frozen case archives without extracting files or executing their contents."""

from __future__ import annotations

import argparse
import json
import tarfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "d01": "docs/experiments/completion/d01/trials/D01-full-1",
    "verifier-budget": "docs/experiments/job-readiness-goal2/b1/trials/M08-v2-full-1",
}
MAX_TRACE_BYTES = 16 * 1024 * 1024


def read_trace(path: Path) -> tuple[str, list[dict[str, Any]]]:
    with tarfile.open(path, "r:gz") as archive:
        members = []
        for index, member in enumerate(archive):
            if index >= 2048:
                raise ValueError("Too many archive members")
            if member.name.startswith(".mokioclaw/traces/") and member.name.endswith("/events.jsonl"):
                members.append(member)
        if len(members) != 1:
            raise ValueError("Expected exactly one canonical trace")
        member = members[0]
        if not member.isfile() or member.size > MAX_TRACE_BYTES:
            raise ValueError("Trace must be a bounded regular file")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError("Trace is unreadable")
        with handle:
            rows = [json.loads(line) for line in handle.read(MAX_TRACE_BYTES + 1).splitlines()]
        if not rows or not all(isinstance(row, dict) for row in rows):
            raise ValueError("Empty or invalid trace")
        return member.name, rows


def inspect_case(repository: Path, case: str) -> dict[str, Any]:
    directory = repository / CASES[case]
    result = json.loads((directory / "result.json").read_text())
    member, rows = read_trace(directory / "trace.tar.gz")
    ends = [row for row in rows if row.get("type") == "run_end"]
    if len(ends) != 1 or ends[0]["run_id"] != result["run_id"]:
        raise ValueError("Trace terminal run ID mismatch")
    if ends[0]["payload"]["status"] != result["execution_status"]:
        raise ValueError("Trace terminal status mismatch")
    streams = []
    milestones = []
    for row in rows:
        wrapper = row.get("payload", {})
        event = wrapper.get("payload", wrapper)
        if row.get("type") == "custom:llm_stream_end":
            streams.append({
                "seq": row["seq"], "stream_id": event.get("stream_id"),
                "node": event.get("node"), "status": event.get("status"),
            })
        if row.get("type") in {
            "custom:completion_budget", "custom:run_budget_exhausted",
            "custom:tool_result", "custom:checkpoint_saved", "run_end",
        }:
            milestones.append({
                "seq": row["seq"], "type": row["type"],
                "node": event.get("node"), "tool_call_id": row.get("tool_call_id"),
                "status": event.get("status"), "stop_reason": event.get("stop_reason"),
            })
    return {
        "schema_version": 1, "case": case, "read_only": True, "real_model_calls": 0,
        "archive": str(Path(CASES[case]) / "trace.tar.gz"), "trace_member": member,
        "run_id": result["run_id"], "frozen_commit": result["config"]["harness_revision"],
        "execution_status": result["execution_status"], "stop_reason": result["stop_reason"],
        "task_solved": result["task_solved"], "grade_valid": result["grade"]["valid"],
        "artifact_passed": result["grade"]["solved"], "tests_run": result["grade"]["tests_run"],
        "requests": result["model_metrics"]["requests"], "tools": result["tool_count"],
        "total_tokens": result["model_metrics"]["total_tokens"],
        "estimated": result["model_metrics"]["estimated"],
        "last_streams": streams[-4:], "last_milestones": milestones[-6:],
        "note": "Metadata only. No archive extraction, model request, tool replay or regrading.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=CASES)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_case(ROOT, args.case), ensure_ascii=False, indent=2))
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        parser.exit(1, f"Case inspection failed: {exc}\n")


if __name__ == "__main__":
    main()
