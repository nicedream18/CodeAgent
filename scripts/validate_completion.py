"""One targeted real trial with the existing independent grader; no matrix rerun."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from dotenv import load_dotenv

from mokioclaw.core.session_lock import session_writer
from mokioclaw.evaluation.models import EvalTrialConfig
from mokioclaw.evaluation.prepare import cache_root, prepare_task, sha, tree_hash
from mokioclaw.evaluation.runner import _verify_frozen, run_trial, write_json

parser = argparse.ArgumentParser()
parser.add_argument("--task", default="E02")
parser.add_argument("--architecture", choices=["full", "baseline", "light", "auto"], default="full")
parser.add_argument("--experiment-id", default="completion-20261017-v1")
parser.add_argument("--cache", type=Path, default=cache_root())
parser.add_argument("--real-model", action="store_true")
args = parser.parse_args()
if not args.real_model:
    print(json.dumps({"real_model": False, "task": args.task, "architecture": args.architecture,
                      "note": "Preview only. --real-model explicitly starts one paid model trial."}))
    raise SystemExit(0)
if not args.experiment_id or any(
    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
    for c in args.experiment_id
):
    raise SystemExit("Invalid experiment ID")
repo = Path(__file__).resolve().parents[1]
load_dotenv(repo / ".env")
if not all(os.environ.get(k) for k in ("API_KEY", "MODEL", "BASE_URL")):
    raise SystemExit("Configure API_KEY, MODEL and BASE_URL before a real trial")
task = prepare_task(args.task, args.cache, repo)
task_file = args.cache / "tasks" / args.task / "task.json"
config = EvalTrialConfig(
    task_id=args.task, architecture=args.architecture, repeat=1, model=os.environ["MODEL"],
    model_config_sha256=sha(json.dumps({
        "model": os.environ["MODEL"], "endpoint": os.environ["BASE_URL"],
        "context_tokens": 32768, "output_tokens": 4096, "sdk_max_retries": 0,
        "parameter": os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens"),
    }, sort_keys=True).encode()),
    harness_revision=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip(),
    prompt_sha256=tree_hash(repo / "src/mokioclaw/prompts"),
)
root = args.cache / "experiments" / args.experiment_id
manifest = {
    "schema_version": 1, "purpose": "targeted_completion_validation_not_architecture_benchmark",
    "harness_revision": config.harness_revision,
    "harness_source_sha256": tree_hash(repo / "src"),
    "lock_sha256": sha((repo / "uv.lock").read_bytes()),
    "prompt_sha256": config.prompt_sha256,
    "model_config_sha256": config.model_config_sha256,
    "tasks": {args.task: sha(task_file.read_bytes())},
    "trials": [{"profile": "targeted", "config": config.model_dump()}],
    "note": "Original benchmark records are unchanged. No automatic retries of started trials.",
}
with session_writer(root):
    receipt = root / "trials" / config.trial_id / "result.json"
    if receipt.exists():
        raise SystemExit("Trial already started; use a distinct experiment ID for a deliberate new attempt")
    freeze = root / "manifest.json"
    if freeze.exists() and json.loads(freeze.read_text()) != manifest:
        raise SystemExit("Frozen trial configuration changed")
    write_json(freeze, manifest)
    _verify_frozen(manifest, repo, task_file)
    result = run_trial(args.experiment_id, config, args.cache, receipt.parent, repo)
    print(json.dumps({
        "experiment_id": args.experiment_id, "trial_id": config.trial_id,
        "status": result.execution_status, "task_solved": result.task_solved,
        "independent_grade": result.grade.model_dump(), "stop_reason": result.stop_reason,
        "requests": result.model_metrics.get("requests"), "result": str(receipt),
    }, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.task_solved else 1)
