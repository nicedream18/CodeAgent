"""Explicit CLI gates for real context-compression experiments."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import typer

from mokioclaw.evaluation import compression_benchmark as benchmark

app = typer.Typer(help="Paired context-compression on/off benchmark; real models require --real-model.")


@app.command("prepare")
def prepare_command(cache: Path | None = None, repository: Path = Path(".")) -> None:
    """Prepare three tasks and run independent baseline/reference Docker checks."""
    try:
        result = benchmark.prepare((cache or benchmark.cache_root()).expanduser().resolve(), repository.resolve())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(result, ensure_ascii=False, indent=2))


@app.command("run")
def run_command(
    experiment_id: str, repeats: int = 3, real_model: bool = False,
    cache: Path | None = None, repository: Path = Path("."),
) -> None:
    """Preview 18 trials by default; --real-model starts the frozen finite matrix."""
    try:
        result = benchmark.run((cache or benchmark.cache_root()).expanduser().resolve(),
                               repository.resolve(), experiment_id,
                               repeats=repeats, real_model=real_model)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({k: v for k, v in result.items() if k not in {"results", "pairs", "pending"}},
                          ensure_ascii=False, indent=2))


@app.command("report")
def report_command(experiment_id: str, cache: Path | None = None) -> None:
    """Read existing receipts and canonical traces; never rerun models or tools."""
    try:
        result = benchmark.report((cache or benchmark.cache_root()).expanduser().resolve(), experiment_id)
    except (OSError, ValueError, KeyError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps({k: v for k, v in result.items() if k not in {"results", "pairs", "pending"}},
                          ensure_ascii=False, indent=2))
