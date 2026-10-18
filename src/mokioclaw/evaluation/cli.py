from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

import typer

from mokioclaw.evaluation.catalog import TASKS
from mokioclaw.evaluation.grader import validate_task
from mokioclaw.evaluation.models import EvalTask
from mokioclaw.evaluation.prepare import cache_root, prepare_suite
from mokioclaw.evaluation.runner import report_experiment, run_experiment

app = typer.Typer(help="Prepare and independently grade the frozen internship code tasks.")

from mokioclaw.evaluation.compression_cli import app as compression_app  # noqa: E402

app.add_typer(compression_app, name="compression")


@app.command("list")
def list_command() -> None:
    for name, item in TASKS.items():
        typer.echo(name + "  " + item.get("instance", item.get("requirement", "")))


@app.command("prepare")
def prepare_command(
    suite: str = "internship-v1",
    cache: Path | None = None,
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    if suite != "internship-v1":
        raise typer.BadParameter("Only internship-v1 is defined")
    root = (cache or cache_root()).expanduser().resolve()
    records = prepare_suite(root, repository.resolve())
    for name, item in records.items():
        if item["prepared"]:
            directory = root / "tasks" / name
            task = EvalTask.model_validate_json((directory / "task.json").read_text())
            item["validation"] = validate_task(task, directory, root / "validation" / name)
        typer.echo(name + ": " + str(item.get("validation", {}).get("valid", False)))
    (root / "preparation.json").write_text(json.dumps(records, indent=2) + "\n")
    if not all(r.get("validation", {}).get("valid") for r in records.values()):
        raise typer.Exit(1)


@app.command("run")
def run_command(
    profile: Literal["core", "coverage"] = "core",
    real_model: bool = False,
    cache: Path | None = None,
    experiment_id: str = "internship-v1-20261016",
    repository: Annotated[Path, typer.Option("--repository")] = Path("."),
) -> None:
    try:
        record = run_experiment(
            (cache or cache_root()).expanduser().resolve(),
            repository.resolve(),
            experiment_id,
            profile,
            real_model=real_model,
        )
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({k: v for k, v in record.items() if k != "results"}, indent=2))


@app.command("report")
def report_command(experiment_id: str, cache: Path | None = None) -> None:
    root = (cache or cache_root()).expanduser().resolve()
    try:
        record = report_experiment(root, experiment_id)
    except (ValueError, OSError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({k: v for k, v in record.items() if k != "results"}, indent=2))
