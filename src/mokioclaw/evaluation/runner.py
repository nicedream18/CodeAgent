"""Single-concurrency experiment journal. Completed attempts are never overwritten."""

from __future__ import annotations

import json
import os
import signal
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from mokioclaw.core.execution_backend import BackendUnavailable, docker_cli
from mokioclaw.evaluation.catalog import CORE, TASKS, schedule
from mokioclaw.evaluation.grader import changed_paths, grade_candidate
from mokioclaw.evaluation.models import EvalTask, EvalTrialConfig, EvalTrialResult
from mokioclaw.evaluation.prepare import copy_source, prepare_task, sha, tree_hash


def now() -> str:
    return datetime.now(UTC).isoformat()


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    with temp.open("w") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    temp.replace(path)


def _manifest(cache: Path, repository: Path) -> dict[str, Any]:
    load_dotenv(repository / ".env")
    model = os.environ.get("MODEL", "")
    endpoint = os.environ.get("BASE_URL", "")
    if not model or not endpoint or not os.environ.get("API_KEY"):
        raise ValueError("Real experiments require MODEL, BASE_URL and API_KEY")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True
    ).strip()
    config_hash = sha(
        json.dumps(
            {
                "model": model,
                "endpoint": endpoint,
                "temperature": 0,
                "context": 32768,
                "output": 4096,
                "parameter": os.getenv("MOKIO_MODEL_OUTPUT_PARAMETER", "max_tokens"),
            },
            sort_keys=True,
        ).encode()
    )
    prompt_hash = tree_hash(repository / "src/mokioclaw/prompts")
    configs = []
    for profile in ("core", "coverage"):
        for task, architecture, repeat in schedule(profile):
            config = EvalTrialConfig(
                task_id=task,
                architecture=architecture,
                repeat=repeat,
                model=model,
                model_config_sha256=config_hash,
                harness_revision=revision,
                prompt_sha256=prompt_hash,
            )
            configs.append({"profile": profile, "config": config.model_dump()})
    tasks = {}
    for name in TASKS:
        prepare_task(name, cache, repository)
        task_path = cache / "tasks" / name / "task.json"
        validation_path = cache / "validation" / name / "validation.json"
        validation = json.loads(validation_path.read_text())
        if not validation.get("valid"):
            raise ValueError(f"Task validation is not valid: {name}")
        tasks[name] = sha(task_path.read_bytes())
    return {
        "schema_version": 1,
        "harness_revision": revision,
        "harness_source_sha256": tree_hash(repository / "src"),
        "lock_sha256": sha((repository / "uv.lock").read_bytes()),
        "prompt_sha256": prompt_hash,
        "model_config_sha256": config_hash,
        "tasks": tasks,
        "trials": configs,
        "seed": 20261016,
        "note": "Custom offline subset; no official SWE-bench score or unseen-task claim.",
    }


def _worker_env(home: Path, repository: Path) -> dict[str, str]:
    # A fresh config/home prevents personal roles, rules, permissions and hooks.
    environment = {
        k: v
        for k, v in os.environ.items()
        if k
        in {
            "PATH",
            "SYSTEMROOT",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "TMPDIR",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "API_KEY",
            "MODEL",
            "BASE_URL",
            "MOKIO_MODEL_OUTPUT_PARAMETER",
        }
    }
    environment.update(
        HOME=str(home),
        PYTHONPATH=str(repository / "src"),
        MOKIO_CONTEXT_TOKEN_LIMIT="32768",
        MOKIO_MODEL_CONTEXT_RESERVE_TOKENS="4096",
        MOKIO_DAEMON_WORKER="1",
        PYTHON_DOTENV_DISABLED="1",
        PYTHONUNBUFFERED="1",
    )
    # The Docker CLI can read its host context; no part of that directory is mounted.
    environment.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    return environment


def _cleanup(owner: str) -> list[str]:
    try:
        ids = docker_cli(
            [
                "ps",
                "-aq",
                "--filter",
                "label=mokioclaw.execution=true",
                "--filter",
                "label=mokioclaw.owner=" + owner,
            ]
        ).stdout.split()
        if ids:
            docker_cli(["rm", "--force", *ids])
        return ids
    except BackendUnavailable:
        return ["cleanup-unconfirmed"]


def _patch(base: Path, candidate: Path) -> str:
    import difflib

    chunks: list[str] = []
    for name in changed_paths(base, candidate):
        if name in {"TODO.md", "NOTEPAD.md", "HISTORY_SUMMARY.md"}:
            continue
        before, after = base / name, candidate / name
        if any(p.is_file() and p.stat().st_size > 1024 * 1024 for p in (before, after)):
            chunks.append(
                f"# Large diff omitted: {name}; source hashes retained in task/grade records.\n"
            )
            continue
        left = (
            before.read_text(errors="replace").splitlines(keepends=True) if before.is_file() else []
        )
        right = (
            after.read_text(errors="replace").splitlines(keepends=True) if after.is_file() else []
        )
        chunks.extend(difflib.unified_diff(left, right, fromfile="a/" + name, tofile="b/" + name))
    return "".join(chunks)


def run_trial(
    experiment_id: str, config: EvalTrialConfig, cache: Path, directory: Path, repository: Path
) -> EvalTrialResult:
    task_root = cache / "tasks" / config.task_id
    task_file = task_root / "task.json"
    task = EvalTask.model_validate_json(task_file.read_text())
    result = EvalTrialResult(
        experiment_id=experiment_id,
        trial_id=config.trial_id,
        config=config,
        task_sha256=sha(task_file.read_bytes()),
        real_model=True,
        execution_status="running",
        started_at=now(),
    )
    path = directory / "result.json"
    write_json(path, result.model_dump())  # Receipt exists before any model request.
    workspace = directory / "workspace"
    home = directory / "home"
    owner = experiment_id + "-" + config.trial_id
    started = time.monotonic()
    try:
        copy_source(task_root / "base", workspace)
        home.mkdir()
        spec = {
            "config": config.model_dump(),
            "workspace": str(workspace),
            "owner": owner,
            "image": task.image_digest,
            "requirement": task.requirement + "\n允许修改范围：" + ", ".join(task.allowed_paths),
            "events": str(directory / "events.jsonl"),
            "output": str(directory / "worker.json"),
        }
        write_json(directory / "spec.json", spec)
        with (directory / "worker.log").open("wb") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "mokioclaw.evaluation.worker", str(directory / "spec.json")],
                cwd=home,
                env=_worker_env(home, repository),
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                process.wait(timeout=config.generation_seconds)
            except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
                result.execution_status = "interrupted"
                result.stop_reason = (
                    "generation_wall_timeout"
                    if isinstance(exc, subprocess.TimeoutExpired)
                    else "user_interrupted"
                )
                os.killpg(process.pid, signal.SIGTERM)
                _cleanup(owner)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        result.generation_seconds = round(time.monotonic() - started, 3)
        worker_path = directory / "worker.json"
        if not worker_path.exists() and worker_path.with_suffix(".progress.json").exists():
            worker_path = worker_path.with_suffix(".progress.json")
        if worker_path.exists():
            worker = json.loads(worker_path.read_text())
            if result.execution_status != "interrupted":
                result.execution_status = (
                    "finished"
                    if worker["status"] == "finished"
                    else "interrupted"
                    if worker["status"] == "interrupted"
                    else "failed"
                )
                result.stop_reason = worker.get("stop_reason")
                if result.stop_reason and result.stop_reason.startswith("provider_"):
                    result.execution_status = "infrastructure_failed"
                    result.infrastructure_error = result.stop_reason
                if worker["status"] == "running":
                    result.execution_status = "infrastructure_failed"
                    result.infrastructure_error = (
                        "Worker exited before finalization; partial metrics retained"
                    )
            result.model_metrics = worker.get("model_metrics", {})
            result.tool_count = worker.get("tool_count", 0)
            result.actual_path = worker.get("actual_path", [])
            result.run_id = worker.get("run_id")
            result.stream_ids = worker.get("stream_ids", [])
            result.tool_call_ids = worker.get("tool_call_ids", [])
            result.model_claim = worker.get("model_claim")
            if worker.get("error"):
                result.artifacts["worker_error"] = str(directory / "worker.json")
        elif result.execution_status != "interrupted":
            result.execution_status = "infrastructure_failed"
            result.infrastructure_error = "Worker exited without a final record"
        # Partial artifacts are graded even after failed/interrupted generation.
        result.grade = grade_candidate(
            task,
            task_root,
            workspace,
            directory / "grade",
            seconds=config.grading_seconds,
            owner=owner + "-grade",
        )
        (directory / "candidate.patch").write_text(_patch(task_root / "base", workspace))
        result.task_solved = result.execution_status == "finished" and result.grade.solved
        result.artifacts.update(
            patch=str(directory / "candidate.patch"),
            trace=str(workspace / ".mokioclaw/traces"),
            events=str(directory / "events.jsonl"),
            grade=str(directory / "grade"),
        )
        cleanup = _cleanup(owner)
        result.safety_assertions = {
            "within_modification_scope": not result.grade.out_of_scope,
            "trusted_tests_unchanged": not result.grade.tampered_tests,
            "no_owned_containers_remaining": "cleanup-unconfirmed" not in cleanup,
            "request_limit_observed": result.model_metrics.get("requests", 0)
            <= config.request_limit,
            "tool_limit_observed": result.tool_count <= config.tool_limit,
        }
        if result.grade.infrastructure_error:
            result.infrastructure_error = result.grade.infrastructure_error
    except Exception as exc:
        result.execution_status = "infrastructure_failed"
        result.infrastructure_error = f"{type(exc).__name__}: {exc}"
        _cleanup(owner)
    finally:
        result.generation_seconds = result.generation_seconds or round(
            time.monotonic() - started, 3
        )
        result.ended_at = now()
        write_json(path, result.model_dump())
    return result


def _run_experiment(
    cache: Path, repository: Path, experiment_id: str, profile: str, *, real_model: bool
) -> dict[str, Any]:
    if not experiment_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in experiment_id
    ):
        raise ValueError("Invalid experiment ID")
    root = cache / "experiments" / experiment_id
    if not real_model:
        root.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": 1,
            "real_model": False,
            "kind": "framework_smoke",
            "profile": profile,
            "scheduled_trials": len(schedule(profile)),
            "task_solved": None,
            "note": "No model request; excluded from real capability statistics.",
        }
        write_json(root / ("offline-" + profile + ".json"), record)
        return record
    manifest = _manifest(cache, repository)
    freeze = root / "manifest.json"
    if freeze.exists():
        if json.loads(freeze.read_text()) != manifest:
            raise ValueError(
                "Frozen source/config/task versions changed; use a distinct experiment ID"
            )
    else:
        write_json(freeze, manifest)
    for item in manifest["trials"]:
        config = EvalTrialConfig.model_validate(item["config"])
        if item["profile"] != profile:
            continue
        directory = root / "trials" / config.trial_id
        receipt = directory / "result.json"
        if receipt.exists():
            previous = EvalTrialResult.model_validate_json(receipt.read_text())
            if previous.execution_status == "running":
                previous.execution_status = "interrupted"
                previous.stop_reason = "runner_interrupted_before_finalization"
                previous.ended_at = now()
                write_json(receipt, previous.model_dump())
            continue  # Resume only trials without a start receipt; never retry.
        result = run_trial(experiment_id, config, cache, directory, repository)
        print(
            f"{config.trial_id}: {result.execution_status}, solved={result.task_solved}, valid={result.grade.valid}",
            flush=True,
        )
        if result.stop_reason == "user_interrupted":
            break
    return report_experiment(cache, experiment_id)


def run_experiment(
    cache: Path, repository: Path, experiment_id: str, profile: str, *, real_model: bool
) -> dict[str, Any]:
    from collections.abc import Callable
    from typing import cast

    from mokioclaw.core.session_lock import session_writer

    if not experiment_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in experiment_id
    ):
        raise ValueError("Invalid experiment ID")
    with cast(Callable[..., Any], session_writer)(cache / "experiments" / experiment_id):
        return _run_experiment(cache, repository, experiment_id, profile, real_model=real_model)


def report_experiment(cache: Path, experiment_id: str) -> dict[str, Any]:
    if not experiment_id or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for c in experiment_id
    ):
        raise ValueError("Invalid experiment ID")
    root = cache / "experiments" / experiment_id
    freeze = root / "manifest.json"
    if not freeze.exists():
        offline = list(root.glob("offline-*.json"))
        if not offline:
            raise ValueError("Unknown experiment")
        return {
            "real_model": False,
            "kind": "framework_smoke",
            "results": [json.loads(p.read_text()) for p in offline],
            "task_solved": None,
        }
    manifest = json.loads(freeze.read_text())
    results = [
        EvalTrialResult.model_validate_json(p.read_text())
        for p in sorted(root.glob("trials/*/result.json"))
    ]
    groups: dict[str, Any] = {}
    for mode in ("baseline", "full", "light", "auto"):
        values = [
            r
            for r in results
            if r.config.architecture == mode and r.real_model and r.config.task_id in CORE
        ]
        durations = [r.generation_seconds for r in values]
        groups[mode] = {
            "completed": len(values),
            "solved": sum(r.task_solved for r in values),
            "seconds": {
                "min": min(durations),
                "median": statistics.median(durations),
                "max": max(durations),
            }
            if durations
            else None,
            "requests": sum(r.model_metrics.get("requests", 0) for r in values),
            "input_tokens": sum(r.model_metrics.get("input_tokens", 0) for r in values),
            "output_tokens": sum(r.model_metrics.get("output_tokens", 0) for r in values),
            "cache_read_tokens": sum(r.model_metrics.get("cache_read_tokens") or 0 for r in values)
            if any(r.model_metrics.get("cache_read_tokens") is not None for r in values)
            else None,
            "cache_creation_tokens": sum(
                r.model_metrics.get("cache_creation_tokens") or 0 for r in values
            )
            if any(r.model_metrics.get("cache_creation_tokens") is not None for r in values)
            else None,
            "cache_usage_incomplete": any(
                r.model_metrics.get("cache_usage_incomplete", True) for r in values
            ),
            "estimated_usage": any(r.model_metrics.get("estimated", True) for r in values),
        }
    repeated = {}
    for task_id in CORE:
        for architecture in ("baseline", "full", "light", "auto"):
            group = [
                r
                for r in results
                if r.config.task_id == task_id and r.config.architecture == architecture
            ]

            def span(values: list[float]) -> dict[str, float] | None:
                return (
                    {"min": min(values), "median": statistics.median(values), "max": max(values)}
                    if values
                    else None
                )

            repeated[task_id + "/" + architecture] = {
                "recorded": len(group),
                "solved": sum(r.task_solved for r in group),
                "generation_seconds": span([r.generation_seconds for r in group]),
                "grading_seconds": span([r.grade.seconds for r in group]),
                "requests": span([float(r.model_metrics.get("requests", 0)) for r in group]),
                "total_tokens": span(
                    [float(r.model_metrics.get("total_tokens", 0)) for r in group]
                ),
            }
    record = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "real_model": True,
        "scheduled": len(manifest["trials"]),
        "recorded": len(results),
        "not_started": len(manifest["trials"]) - len(results),
        "solved": sum(r.task_solved for r in results),
        "invalid_grades": sum(not r.grade.valid for r in results),
        "regression_failures": sum(r.grade.regression_failed > 0 for r in results),
        "out_of_scope": sum(bool(r.grade.out_of_scope) for r in results),
        "budget_stops": sum(bool(r.stop_reason and "budget" in r.stop_reason) for r in results),
        "infrastructure_failures": sum(bool(r.infrastructure_error) for r in results),
        "model_claim_disagreements": sum(
            r.model_claim is not None and r.model_claim != r.grade.solved for r in results
        ),
        "core_architectures": groups,
        "core_repeats": repeated,
        "coverage": {
            "recorded": sum(r.config.task_id not in CORE for r in results),
            "solved": sum(r.task_solved and r.config.task_id not in CORE for r in results),
        },
        "results": [r.model_dump() for r in results],
    }
    write_json(root / "report.json", record)
    lines = [
        f"# {experiment_id}",
        "",
        f"真实模型试验：{len(results)}/{len(manifest['trials'])}；解决 {record['solved']}；基础设施失败 {record['infrastructure_failures']}。",
        "",
        "| 架构 | 已记录 | 解决 | 请求 | 生成耗时 min/median/max(s) |",
        "|---|---:|---:|---:|---|",
    ]
    for mode, group in groups.items():
        lines.append(
            f"| {mode} | {group['completed']} | {group['solved']} | {group['requests']} | {group['seconds']} |"
        )
    lines += [
        "",
        "成功由独立测试判断，安全停止不计成功。基础设施失败仍保留在预定分母中。",
        "公开任务可能见于训练数据，不声称验证了未见任务泛化；本结果不是官方 SWE-bench 成绩。",
    ]
    (root / "report.md").write_text("\n".join(lines) + "\n")
    return record
