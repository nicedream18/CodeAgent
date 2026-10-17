"""Reconstruct changed bytes and grade them; never repeat a model attempt."""

import argparse
import json
import re
import shutil
import tarfile
import tempfile
from pathlib import Path

from mokioclaw.evaluation.grader import grade_candidate
from mokioclaw.evaluation.models import EvalTask
from mokioclaw.evaluation.prepare import cache_root, copy_source, file_hashes, prepare_task

parser = argparse.ArgumentParser()
parser.add_argument("trial_id")
parser.add_argument("--archive", type=Path, default=Path("docs/experiments/internship-v1/main"))
parser.add_argument("--cache", type=Path, default=cache_root())
parser.add_argument("--output", type=Path)
args = parser.parse_args()
if not re.fullmatch(r"[A-Za-z0-9_-]+", args.trial_id):
    raise SystemExit("Invalid trial ID")
source = args.archive / "trials" / args.trial_id
record = json.loads((source / "result.json").read_text())
task_id = record["config"]["task_id"]
task = prepare_task(task_id, args.cache, Path.cwd())
original_task = EvalTask.model_validate_json(
    (args.archive.parent / "tasks" / f"{task_id}.json").read_text()
)
if (task.source_sha256, task.dependency_sha256, task.target_tests, task.regression_tests) != (
    original_task.source_sha256,
    original_task.dependency_sha256,
    original_task.target_tests,
    original_task.regression_tests,
):
    raise SystemExit("Source/dependency/test contract differs from the archived experiment")
output = args.output or Path(tempfile.mkdtemp(prefix="mokio-reproduce-"))
workspace = output / "workspace"
if workspace.exists():
    raise SystemExit("Output workspace must be new; existing files will not be overwritten")
copy_source(args.cache / "tasks" / task_id / "base", workspace)
changes = json.loads((source / "file-changes.json").read_text())
for name, item in changes.items():
    path = (workspace / name).resolve()
    if not path.is_relative_to(workspace.resolve()):
        raise SystemExit("Unsafe archived path")
    if item["after"] is None:
        path.unlink(missing_ok=True)
with tarfile.open(source / "changed-files.tar.gz") as archive:
    archive.extractall(workspace, filter="data")
reference_files = output / "reference"
shutil.copytree(source / "grade", reference_files)
restored = file_hashes(workspace)
for name, item in changes.items():
    if restored.get(name) != item["after"]:
        raise SystemExit("Reconstructed artifact hash mismatch: " + name)
grade = grade_candidate(task, args.cache / "tasks" / task_id, workspace, output / "regrade")
result = {
    "real_model": False,
    "kind": "independent_regrade",
    "original_run_status": record["execution_status"],
    "original_task_solved": record["task_solved"],
    "original_artifact_passed": record["grade"]["solved"],
    "regrade": grade.model_dump(),
    "same_test_outcome": grade.valid == record["grade"]["valid"]
    and grade.solved == record["grade"]["solved"],
    "image_changed": task.image_digest != original_task.image_digest,
    "output": str(output),
}
(output / "reproduction.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(result, ensure_ascii=False, indent=2))
raise SystemExit(0 if result["same_test_outcome"] else 1)
