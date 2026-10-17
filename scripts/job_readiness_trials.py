"""External fixed-matrix coordinator; frozen runtimes are never imported into each other."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv

from mokioclaw.core.session_lock import session_writer
from mokioclaw.evaluation.job_tasks import job_cache_root, prepare_job_task
from mokioclaw.evaluation.models import EvalTrialConfig
from mokioclaw.evaluation.prepare import sha
from mokioclaw.evaluation.process_control import command_for
from mokioclaw.evaluation.runner import _source_contract, _worker_env, now, write_json

REPOSITORY = Path(__file__).resolve().parents[1]
INPUT = REPOSITORY / "docs/experiments/job-readiness-v1/goal2-input.json"
IMAGE = "sha256:122a00775cb27f1e8fc02b10ca32b4466d21f5e14df7964c6de0436dce9d48ba"


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def fixed_input() -> dict[str, Any]:
    plan: dict[str, Any] = read(INPUT)
    combinations = {(t["version"], t["task_id"], t["repeat"]) for t in plan["trials"]}
    wanted = {(v, t, r) for v in ("B0", "B1")
              for t in ("M02-v2", "M07-v2", "M08-v2") for r in (1, 2, 3)}
    if len(plan["trials"]) != 18 or combinations != wanted or plan["concurrency"] != 1:
        raise ValueError("Fixed 18-trial plan changed")
    return plan


def verify_version(checkout: Path, version: dict[str, Any]) -> dict[str, str]:
    actual = _source_contract(checkout)
    wanted = {"harness_revision": version["revision"],
              "harness_source_sha256": version["source_sha256"],
              "prompt_sha256": version["prompt_sha256"], "lock_sha256": version["lock_sha256"]}
    if actual != wanted:
        raise ValueError(f"Frozen checkout mismatch: {checkout}")
    status = subprocess.check_output(["git", "status", "--porcelain"], cwd=checkout, text=True)
    if status.strip():
        raise ValueError(f"Frozen checkout has changes: {checkout}")
    return actual


def verify_tasks(cache: Path, plan: dict[str, Any]) -> dict[str, Any]:
    evidence = REPOSITORY / "docs/experiments/job-readiness-v1"
    for relative, digest in read(evidence / "task-artifacts.json").items():
        if sha((evidence / relative).read_bytes()) != digest:
            raise ValueError("Archived task evidence changed: " + relative)
    checked = {}
    for name, digest in plan["versions"]["B0"]["task_hashes"].items():
        task = prepare_job_task(name, cache, REPOSITORY)
        if sha((cache / "tasks" / name / "task.json").read_bytes()) != digest:
            raise ValueError("Cached task contract changed: " + name)
        validation = read(evidence / "tasks" / name / "validation/validation.json")
        if (task.image_digest != IMAGE or not validation["valid"]
                or not validation["visible"]["ok"] or validation["visible"]["exit_code"]
                or not validation["base"]["target_failed"]
                or not validation["reference"]["solved"]):
            raise ValueError("Missing credible task validation: " + name)
        checked[name] = {"task_sha256": digest, "source_sha256": task.source_sha256,
                         "image": task.image_digest, "validation": validation}
    return checked


def import_probe(checkout: Path) -> dict[str, Any]:
    # Same executable, fresh HOME/cwd and exact environment constructor used by
    # the original run_trial when it execs its worker. No new runtime is injected.
    with TemporaryDirectory(prefix="goal2-import-") as temp:
        home = Path(temp)
        code = """import json, pathlib, sys
from mokioclaw.evaluation import worker, runner
from mokioclaw.core import agent
from mokioclaw.agents import code_agent
from mokioclaw.providers import openai_provider
modules = [worker, runner, agent, code_agent, openai_provider]
print(json.dumps({'executable': sys.executable,
 'modules': {m.__name__: str(pathlib.Path(m.__file__).resolve()) for m in modules}}))
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=home,
                                env=_worker_env(home, checkout), capture_output=True,
                                text=True, check=True, timeout=30)
        probe: dict[str, Any] = json.loads(result.stdout)
        if not all(Path(p).is_relative_to(checkout / "src") for p in probe["modules"].values()):
            raise ValueError("Worker import resolved outside the frozen checkout")
        return probe


def preflight(cache: Path, plan: dict[str, Any], *, offline: bool) -> dict[str, Any]:
    record: dict[str, Any] = {"recorded_at": now(), "real_model": False,
                              "input_sha256": sha(INPUT.read_bytes()), "versions": {}}
    record["tasks"] = verify_tasks(cache, plan)
    subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                   check=True, capture_output=True, timeout=15)
    image = subprocess.check_output(["docker", "image", "inspect", IMAGE,
                                    "--format", "{{.Id}} {{.Architecture}}"], text=True).strip()
    record["docker_image"] = image
    for label, version in plan["versions"].items():
        checkout = cache / "checkouts" / label
        version_record: dict[str, Any] = verify_version(checkout, version)
        version_record["import_probe"] = import_probe(checkout)
        if offline:
            controls = {}
            for kind in ("requests", "tokens", "tools"):
                environment = _worker_env(cache, checkout)
                environment.pop("API_KEY", None)
                result = subprocess.run(
                    [sys.executable, str(REPOSITORY / "examples/completion_budget_demo.py"),
                     "--kind", kind], cwd=checkout, env=environment,
                    text=True, capture_output=True, check=True, timeout=60)
                controls[kind] = json.loads(result.stdout)
                if not Path(controls[kind]["implementation_source"]).is_relative_to(checkout):
                    raise ValueError("Offline control imported wrong version")
            version_record["offline_controls"] = controls
        record["versions"][label] = version_record
    return record


def config_for(item: dict[str, Any], plan: dict[str, Any], model_hash: str) -> EvalTrialConfig:
    version = plan["versions"][item["version"]]
    return EvalTrialConfig(task_id=item["task_id"], architecture=item["architecture"],
                           repeat=item["repeat"], seed=plan["seed"], model=os.environ["MODEL"],
                           model_config_sha256=model_hash, harness_revision=version["revision"],
                           prompt_sha256=version["prompt_sha256"], **plan["budgets"])


def action_for(directory: Path) -> str:
    receipt, claim = directory / "result.json", directory / "dispatch.json"
    if receipt.exists() and read(receipt)["execution_status"] not in {"running", "not_started"}:
        return "skip_terminal"
    if receipt.exists() or claim.exists():
        observations = {}
        for name in ("dispatch-process.json", "worker-process.json"):
            if (directory / name).exists():
                identity = read(directory / name)
                observations[name] = {**identity, "current_command": command_for(identity["pid"])}
        # A timeout, stale lock, or absent final output never authorizes a retry.
        raise RuntimeError("Started trial requires process/owner audit; will not rerun: "
                           + str(directory) + " " + json.dumps(observations))
    return "start"


def dispatch(spec: dict[str, Any], directory: Path, checkout: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "dispatch.json").open("x") as handle:
        json.dump({**spec, "claimed_at": now()}, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    spec_path = directory / "dispatch.json"
    with (directory / "dispatch.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                                    "--dispatch", str(spec_path), "--nonce", spec["nonce"]],
                                   cwd=checkout, env={**os.environ, "PYTHONPATH": str(checkout / "src"),
                                                     "PYTHON_DOTENV_DISABLED": "1"},
                                   stdout=log, stderr=log)
        write_json(directory / "dispatch-process.json", {"pid": process.pid, "nonce": spec["nonce"]})
        # Original run_trial owns the 900s worker timeout and scoped cleanup.
        process.wait()
    if process.returncode:
        raise RuntimeError("Dispatcher failed; inspect retained receipt/log, no retry")


def dispatch_main(path: Path, nonce: str) -> None:
    from mokioclaw.evaluation.runner import run_trial

    spec = read(path)
    if spec["nonce"] != nonce:
        raise ValueError("Dispatch identity mismatch")
    checkout, cache = Path(spec["checkout"]), Path(spec["cache"])
    verify_version(checkout, spec["version_contract"])
    # This function and run_trial were imported by an interpreter with frozen
    # PYTHONPATH; worker subsequently execs with that version's _worker_env.
    if not Path(sys.modules[run_trial.__module__].__file__ or "").resolve().is_relative_to(checkout):
        raise ValueError("run_trial loaded from the wrong checkout")
    write_json(path.parent / "worker-import-contract.json", import_probe(checkout))
    config = EvalTrialConfig.model_validate(spec["config"])
    if (path.parent / "result.json").exists():
        raise ValueError("Original run_trial may only be called once")
    result = run_trial(spec["experiment_id"], config, cache, path.parent, checkout)
    verify_version(checkout, spec["version_contract"])
    print(json.dumps({"trial_id": result.trial_id, "status": result.execution_status,
                      "task_solved": result.task_solved}), flush=True)


def run_matrix(cache: Path, batch: str, plan: dict[str, Any]) -> None:
    load_dotenv(REPOSITORY / ".env")
    if not all(os.environ.get(k) for k in ("MODEL", "BASE_URL", "API_KEY")):
        raise ValueError("MODEL/BASE_URL/API_KEY required; values are never logged")
    model_hash = sha(json.dumps({"model": os.environ["MODEL"], "endpoint": os.environ["BASE_URL"],
                                "temperature": 0, "context_tokens": 32768, "output_tokens": 4096,
                                "sdk_max_retries": 0,
                                "parameter": os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens")},
                               sort_keys=True).encode())
    root = cache / "batches" / batch
    with session_writer(root):
        checked = preflight(cache, plan, offline=False)
        if not (root / "offline-preflight.json").exists():
            raise ValueError("Run --preflight before any real trial")
        prior = read(root / "offline-preflight.json")
        if prior["input_sha256"] != checked["input_sha256"] or not all(
            prior["versions"][v].get("offline_controls") for v in ("B0", "B1")
        ):
            raise ValueError("Offline preflight missing or stale")
        write_json(root / "latest-preflight.json", checked)
        configs = [config_for(item, plan, model_hash) for item in plan["trials"]]
        for label, version in plan["versions"].items():
            experiment = batch + "-" + label.lower()
            manifest = {"schema_version": 1, "purpose": "goal2_fixed_18_real_trials",
                        **verify_version(cache / "checkouts" / label, version),
                        "model_config_sha256": model_hash, "tasks": version["task_hashes"],
                        "trials": [{"profile": "goal2", "config": c.model_dump()}
                                   for t, c in zip(plan["trials"], configs, strict=True)
                                   if t["version"] == label],
                        "authorization": {"real_model_authorized": True, "max_trials_total": 18,
                                          "source": "User Goal 2 objective, 2026-10-17",
                                          "no_started_retries": True},
                        "input_sha256": checked["input_sha256"], "seed": plan["seed"]}
            target = cache / "experiments" / experiment / "manifest.json"
            if target.exists() and read(target) != manifest:
                raise ValueError("Frozen experiment manifest changed")
            if not target.exists():
                write_json(target, manifest)
        for index, (item, config) in enumerate(zip(plan["trials"], configs, strict=True), 1):
            label = item["version"]
            experiment = batch + "-" + label.lower()
            directory = cache / "experiments" / experiment / "trials" / config.trial_id
            if action_for(directory) == "skip_terminal":
                continue
            # Check source, immutable task/validation materials and engine before
            # every new start, with no paid health-probe requests.
            verify_tasks(cache, plan)
            checkout = cache / "checkouts" / label
            verify_version(checkout, plan["versions"][label])
            subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                           capture_output=True, check=True, timeout=15)
            print(f"START {index}/18 {label} {config.trial_id}", flush=True)
            dispatch({"experiment_id": experiment, "config": config.model_dump(),
                      "checkout": str(checkout), "cache": str(cache), "schedule_index": index,
                      "version_contract": plan["versions"][label], "nonce": uuid4().hex},
                     directory, checkout)
            result = read(directory / "result.json")
            print(json.dumps({"index": index, "version": label, "trial_id": config.trial_id,
                              "status": result["execution_status"], "task_solved": result["task_solved"],
                              "artifact_passed": result["grade"]["solved"],
                              "requests": result["model_metrics"].get("requests"),
                              "stop_reason": result["stop_reason"]}), flush=True)
            if result["execution_status"] in {"running", "not_started"}:
                raise RuntimeError("No terminal receipt; no further starts")
            if result["infrastructure_error"] or result["execution_status"] == "infrastructure_failed":
                raise RuntimeError("Infrastructure failure: inspect before resuming unstarted trials")
            if result["stop_reason"] == "user_interrupted":
                break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=job_cache_root())
    parser.add_argument("--batch", default="job-readiness-goal2-20261017")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--real-model", action="store_true")
    parser.add_argument("--dispatch", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--nonce", default="", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.dispatch:
        dispatch_main(args.dispatch.resolve(), args.nonce)
        return
    if not args.batch or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in args.batch):
        raise ValueError("Invalid batch name")
    plan = fixed_input()
    if args.preflight:
        target = args.cache / "batches" / args.batch / "offline-preflight.json"
        record = preflight(args.cache, plan, offline=True)
        write_json(target, record)
        print(json.dumps({"real_model": False, "offline_preflight": str(target)}))
    elif args.real_model:
        run_matrix(args.cache, args.batch, plan)
    else:
        print(json.dumps({"real_model": False, "scheduled": 18, "plan": str(INPUT),
                          "note": "Preview only; no model or Docker started."}))


if __name__ == "__main__":
    main()
