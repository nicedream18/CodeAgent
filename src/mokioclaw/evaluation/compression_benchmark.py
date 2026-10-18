"""Frozen, paired on/off context-compression experiments using the existing grader."""

from __future__ import annotations

import json
import os
import random
import re
import statistics
import subprocess
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mokioclaw.core.session_lock import session_writer
from mokioclaw.evaluation.completion_demo import prepare_demo_task
from mokioclaw.evaluation.compression_metrics import markdown, measure_trial, summarize
from mokioclaw.evaluation.grader import validate_task
from mokioclaw.evaluation.job_tasks import prepare_job_task, validate_job_task
from mokioclaw.evaluation.models import EvalTask, EvalTrialConfig
from mokioclaw.evaluation.prepare import sha, tree_hash
from mokioclaw.evaluation.runner import _source_contract, run_trial, write_json

TASKS = ("D01", "M07-v2", "M08-v2")
SEED = 20261018


def cache_root() -> Path:
    return Path.home() / ".cache/mokioclaw/eval/context-compression-v1"


def schedule(repeats: int = 3) -> list[dict[str, Any]]:
    if repeats not in {1, 3}:
        raise ValueError("repeats must be 1 (smoke) or 3 (main benchmark)")
    randomizer = random.Random(SEED)
    pairs = [(task, repeat) for task in TASKS for repeat in range(1, repeats + 1)]
    randomizer.shuffle(pairs)
    order = list(TASKS)
    randomizer.shuffle(order)
    starts_on = {task: index % 2 == 0 for index, task in enumerate(order)}
    rows = []
    for task, repeat in pairs:
        first = starts_on[task] if repeat % 2 else not starts_on[task]
        arms = [first, not first]
        for enabled in arms:
            rows.append({"task_id": task, "repeat": repeat,
                         "arm": "on" if enabled else "off", "enabled": enabled})
    return rows


def prepare(cache: Path, repository: Path) -> dict[str, Any]:
    """Model-free preparation; actual baseline/reference tests run in Docker."""
    records = {}
    cache.mkdir(parents=True, exist_ok=True)
    with session_writer(cache):
        for name in TASKS:
            task = prepare_demo_task(cache) if name == "D01" else prepare_job_task(name, cache, repository)
            path = cache / "validation" / name / "validation.json"
            if path.exists():
                validation = json.loads(path.read_text())
            else:
                validation = (
                    validate_task(task, cache / "tasks" / name, cache / "validation" / name)
                    if name == "D01" else validate_job_task(task, cache)
                )
                write_json(path, validation)
            if not (validation.get("valid") and validation.get("base", {}).get("target_failed", 0) > 0
                    and validation.get("reference", {}).get("solved")
                    and validation.get("reference", {}).get("valid")
                    and validation.get("reference", {}).get("tests_run", 0) > 0):
                raise ValueError("Baseline/reference validation failed: " + name)
            records[name] = {
                "task_sha256": sha((cache / "tasks" / name / "task.json").read_bytes()),
                "materials_sha256": tree_hash(cache / "tasks" / name),
                "validation_sha256": sha(path.read_bytes()), "image": task.image_digest,
            }
        record = {"schema_version": 1, "suite": "context-compression-v1",
                  "tasks": records, "real_model_calls": 0}
        target = cache / "preparation.json"
        if target.exists() and json.loads(target.read_text()) != record:
            raise ValueError("Frozen preparation changed; use a new cache")
        write_json(target, record)
    return record


def verify_tasks(cache: Path, preparation: dict[str, Any]) -> None:
    for name, record in preparation["tasks"].items():
        task_root = cache / "tasks" / name
        validation = cache / "validation" / name / "validation.json"
        if (sha((task_root / "task.json").read_bytes()) != record["task_sha256"]
                or tree_hash(task_root) != record["materials_sha256"]
                or sha(validation.read_bytes()) != record["validation_sha256"]):
            raise ValueError("Frozen task or validation changed: " + name)
        task = EvalTask.model_validate_json((task_root / "task.json").read_text())
        if tree_hash(task_root / "base") != task.source_sha256:
            raise ValueError("Task source hash mismatch: " + name)


def _verify_checkout(checkout: Path, manifest: dict[str, Any]) -> None:
    actual = _source_contract(checkout)
    if actual != {k: manifest[k] for k in actual}:
        raise ValueError("Frozen benchmark source changed")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True).strip():
        raise ValueError("Frozen benchmark checkout is dirty")


def model_hash() -> str:
    if not all(os.getenv(k) for k in ("MODEL", "BASE_URL", "API_KEY")):
        raise ValueError("Real A/B trials require MODEL, BASE_URL and API_KEY; values are not logged")
    return sha(json.dumps({"model": os.environ["MODEL"], "endpoint": os.environ["BASE_URL"],
                           "temperature": 0, "context": 32768, "output": 4096,
                           "sdk_max_retries": 0,
                           "parameter": os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens")},
                          sort_keys=True).encode())


def _manifest(cache: Path, repository: Path, experiment_id: str, repeats: int) -> dict[str, Any]:
    load_dotenv(repository / ".env")
    config_hash = model_hash()
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=repository, text=True).strip():
        raise ValueError("Commit benchmark code before starting real trials")
    prepared = json.loads((cache / "preparation.json").read_text())
    verify_tasks(cache, prepared)
    source = _source_contract(repository)
    trials = []
    for item in schedule(repeats):
        config = EvalTrialConfig(
            task_id=item["task_id"], architecture="full", repeat=item["repeat"], seed=SEED,
            context_compression_enabled=item["enabled"], model=os.environ["MODEL"],
            model_config_sha256=config_hash, harness_revision=source["harness_revision"],
            prompt_sha256=source["prompt_sha256"],
        )
        trials.append({**item, "experiment_id": experiment_id + "-" + item["arm"],
                       "config": config.model_dump()})
    return {"schema_version": 1, "experiment_id": experiment_id, **source,
            "model_config_sha256": config_hash, "preparation": prepared,
            "seed": SEED, "repeats": repeats, "concurrency": 1, "trials": trials,
            "policy": {"architecture": "full", "context_window": 32768, "output_reserve": 4096,
                       "safety_tokens": 1024, "high_ratio": 0.85, "target_ratio": 0.60,
                       "tool_output_clipping": "identical in both arms",
                       "compression_requests_in_total_budget": True,
                       "no_automatic_retries_of_started_trials": True}}


def trial_directory(cache: Path, item: dict[str, Any]) -> Path:
    return cache / "experiments" / str(item["experiment_id"]) / "trials" / (
        EvalTrialConfig.model_validate(item["config"]).trial_id
    )


def report(cache: Path, experiment_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", experiment_id):
        raise ValueError("Invalid benchmark experiment ID")
    root = cache / "benchmarks" / experiment_id
    manifest = json.loads((root / "manifest.json").read_text())
    rows = []
    pending = []
    for item in manifest["trials"]:
        directory = trial_directory(cache, item)
        if (directory / "result.json").exists():
            if json.loads((directory / "result.json").read_text())["execution_status"] in {"running", "not_started"}:
                pending.append({**item, "started": True})
                continue
            row = measure_trial(directory, item["enabled"])
            rows.append({**row, "arm": item["arm"], "experiment_id": item["experiment_id"]})
        else:
            pending.append({**item, "started": directory.exists()})
    arms = {arm: summarize([r for r in rows if r["arm"] == arm], 3 * manifest["repeats"])
            for arm in ("on", "off")}
    tasks = {name: {arm: summarize([r for r in rows if r["arm"] == arm and r["task_id"] == name],
                                  manifest["repeats"]) for arm in ("on", "off")} for name in TASKS}
    pairs = []
    for task in TASKS:
        for repeat in range(1, manifest["repeats"] + 1):
            pair = {r["arm"]: r for r in rows if r["task_id"] == task and r["repeat"] == repeat}
            if pair.keys() == {"on", "off"}:
                pairs.append({"task_id": task, "repeat": repeat,
                              "both_solved": pair["on"]["task_solved"] and pair["off"]["task_solved"],
                              "tokens_on_minus_off": pair["on"]["total_tokens"] - pair["off"]["total_tokens"],
                              "generation_seconds_on_minus_off": round(pair["on"]["generation_seconds"] - pair["off"]["generation_seconds"], 3)})
    value = {
        "schema_version": 1, "experiment_id": experiment_id,
        "harness_revision": manifest["harness_revision"], "real_model": True,
        "planned": len(manifest["trials"]), "recorded": len(rows),
        "complete": all(a["terminal"] == a["planned"] for a in arms.values()),
        "measurement_valid": bool(rows) and all(a["measurement_valid"] for a in arms.values()),
        "arms": arms, "tasks": tasks, "pairs": pairs, "results": rows, "pending": pending,
        "paired_token_delta_median": statistics.median([p["tokens_on_minus_off"] for p in pairs]) if pairs else None,
        "limits": ["Compression token and latency overhead included; cache reads not added to input twice.",
                   "Estimated usage is not precise API billing; missing cache creation remains unknown.",
                   "Failure stopping time is not successful task latency; D01 is a controlled short task.",
                   "No significance claim from three repeats; remote prompt caching is measured but not controlled."]}
    write_json(root / "report.json", value)
    (root / "report.md").write_text(markdown(value))
    return value


def run(cache: Path, repository: Path, experiment_id: str, *, repeats: int = 3,
        real_model: bool = False) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", experiment_id):
        raise ValueError("Invalid benchmark experiment ID")
    planned = schedule(repeats)
    if not real_model:
        return {"real_model": False, "mode": "preview", "scheduled": len(planned),
                "trials": planned, "metrics": None,
                "note": "Preview only; no environment secrets, model requests or containers used."}
    root = cache / "benchmarks" / experiment_id
    root.mkdir(parents=True, exist_ok=True)
    with session_writer(root):
        path = root / "manifest.json"
        checkout = root / "checkout"
        if path.exists():
            manifest = json.loads(path.read_text())
            if manifest["repeats"] != repeats:
                raise ValueError("Frozen repeat count changed")
            load_dotenv(repository / ".env")
            current = _source_contract(repository)
            if any(current[k] != manifest[k] for k in current if k != "harness_revision"):
                raise ValueError("Coordinator code differs from frozen benchmark")
        else:
            manifest = _manifest(cache, repository, experiment_id, repeats)
            write_json(path, manifest)
            subprocess.run(["git", "clone", "--no-hardlinks", "--no-checkout", str(repository), str(checkout)],
                           check=True, capture_output=True, timeout=120)
            subprocess.run(["git", "checkout", "--detach", manifest["harness_revision"]],
                           cwd=checkout, check=True, capture_output=True, timeout=60)
            for arm in ("on", "off"):
                group = {**manifest, "experiment_id": experiment_id + "-" + arm,
                         "trials": [t for t in manifest["trials"] if t["arm"] == arm]}
                write_json(cache / "experiments" / group["experiment_id"] / "manifest.json", group)
        for index, item in enumerate(manifest["trials"], 1):
            directory = trial_directory(cache, item)
            if directory.exists():
                receipt = directory / "result.json"
                if receipt.exists() and json.loads(receipt.read_text())["execution_status"] not in {"running", "not_started"}:
                    continue
                raise ValueError("Started trial has no terminal receipt; inspect it, never automatically rerun: " + str(directory))
            _verify_checkout(checkout, manifest)
            verify_tasks(cache, manifest["preparation"])
            if model_hash() != manifest["model_config_sha256"]:
                raise ValueError("Model endpoint/options changed; no new trial started")
            subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           check=True, capture_output=True, timeout=15)
            config = EvalTrialConfig.model_validate(item["config"])
            directory.mkdir(parents=True)
            print(f"START {index}/{len(planned)} {item['arm']} {config.trial_id}", flush=True)
            result = run_trial(item["experiment_id"], config, cache, directory, checkout)
            measured = report(cache, experiment_id)
            print(json.dumps({"position": index, "arm": item["arm"], "trial_id": config.trial_id,
                              "status": result.execution_status, "solved": result.task_solved,
                              "tokens": result.model_metrics.get("total_tokens"),
                              "generation_seconds": result.generation_seconds}), flush=True)
            if not measured["measurement_valid"]:
                raise RuntimeError("Measurement mismatch retained; stop before starting another trial")
            if result.infrastructure_error or result.execution_status == "infrastructure_failed":
                raise RuntimeError("Infrastructure failure retained; inspect before continuing unstarted trials")
            if result.stop_reason == "user_interrupted":
                break
    return report(cache, experiment_id)
