"""Archive frozen receipts and changed bytes without rerunning any model/tool."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path

from mokioclaw.evaluation.prepare import cache_root, file_hashes

parser = argparse.ArgumentParser()
parser.add_argument("experiment_id")
parser.add_argument("--cache", type=Path, default=cache_root())
parser.add_argument("--output", type=Path, default=Path("docs/experiments/internship-v1/main"))
args = parser.parse_args()
source = args.cache / "experiments" / args.experiment_id
output = args.output
output.mkdir(parents=True, exist_ok=True)
manifest = json.loads((source / "manifest.json").read_text())
existing = output / "manifest.json"
if existing.exists() and json.loads(existing.read_text()) != manifest:
    raise SystemExit("Archive belongs to another frozen experiment; choose a new --output")
(output / "archive-info.json").write_text(
    json.dumps({"experiment_id": args.experiment_id, "source": str(source)}, indent=2) + "\n"
)
for name in (
    "manifest.json", "report.json", "report.md", "validity.json",
    "audit.json", "audit.initial-strict.json",
):
    if (source / name).exists():
        shutil.copy2(source / name, output / name)
index = []
for receipt in sorted(source.glob("trials/*/result.json")):
    result = json.loads(receipt.read_text())
    if result["execution_status"] in {"running", "not_started"}:
        continue
    trial = receipt.parent
    workspace = trial / "workspace"
    base = args.cache / "tasks" / result["config"]["task_id"] / "base"
    original, candidate = file_hashes(base), file_hashes(workspace)
    changes = [
        name
        for name in sorted(original.keys() | candidate.keys())
        if original.get(name) != candidate.get(name)
    ]
    changed = {
        name: {"before": original.get(name), "after": candidate.get(name)} for name in changes
    }
    events = []
    if (trial / "events.jsonl").exists():
        for line in (trial / "events.jsonl").read_text().splitlines():
            try:
                event = json.loads(line).get("event", {})
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                pass
    paths = []
    for event in events:
        if event.get("type") == "llm_stream_start":
            node = event.get("node") or event.get("agent")
            if node and (not paths or paths[-1] != node):
                paths.append(node)
        if event.get("type") == "workflow_selected":
            paths.append(
                "workflow:"
                + str(event.get("mode"))
                + (":upgraded" if event.get("upgraded") else "")
            )
    target = output / "trials" / trial.name
    target.mkdir(parents=True, exist_ok=True)
    (target / "file-changes.json").write_text(
        json.dumps(changed, ensure_ascii=False, indent=2) + "\n"
    )
    for name in ("result.json", "worker.json", "candidate.patch"):
        if (trial / name).exists():
            shutil.copy2(trial / name, target / name)
    shutil.copytree(trial / "grade", target / "grade", dirs_exist_ok=True)
    with tarfile.open(target / "changed-files.tar.gz", "w:gz") as archive:
        for name in changes:
            if (workspace / name).is_file():
                archive.add(workspace / name, arcname=name, recursive=False)
    with tarfile.open(target / "trace.tar.gz", "w:gz") as archive:
        for name in ("events.jsonl", "worker.log", "worker.progress.json"):
            if (trial / name).exists():
                archive.add(trial / name, arcname=name)
        for name in ("traces", "diagnostics"):
            if (workspace / ".mokioclaw" / name).exists():
                archive.add(workspace / ".mokioclaw" / name, arcname=".mokioclaw/" + name)
        checkpoints = workspace / ".mokioclaw/checkpoints"
        if checkpoints.exists():
            for path in checkpoints.iterdir():
                if path.is_file():
                    archive.add(path, arcname=".mokioclaw/checkpoints/" + path.name)
    archive_hashes = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in target.iterdir() if p.is_file()
    }
    error = (
        json.loads((trial / "worker.json").read_text()).get("error")
        if (trial / "worker.json").exists()
        else None
    )
    index.append(
        {
            "trial_id": trial.name,
            "task": result["config"]["task_id"],
            "architecture": result["config"]["architecture"],
            "run_status": result["execution_status"],
            "task_solved": result["task_solved"],
            "independent_artifact_passed": result["grade"]["solved"],
            "grade_valid": result["grade"]["valid"],
            "stop_reason": result.get("stop_reason"),
            "worker_error": error,
            "infrastructure_error": result.get("infrastructure_error"),
            "observed_model_path": paths,
            "requests": result["model_metrics"].get("requests"),
            "total_tokens": result["model_metrics"].get("total_tokens"),
            "generation_seconds": result["generation_seconds"],
            "grading_seconds": result["grade"]["seconds"],
            "archive_sha256": archive_hashes,
        }
    )
(output / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n")
print(
    json.dumps(
        {"frozen_trials": len(manifest["trials"]), "archived": len(index), "output": str(output)},
        ensure_ascii=False,
    )
)
