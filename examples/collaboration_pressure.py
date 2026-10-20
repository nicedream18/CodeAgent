"""Offline finite pressure experiment; no real model or external service calls."""

import argparse
import json
from pathlib import Path

from mokioclaw.evaluation.context_pressure import run_suite

parser = argparse.ArgumentParser()
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--execution-backend", choices=["local", "docker"], default="local")
parser.add_argument("--docker-image", default="mokioclaw-execution:0.2.0")
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
result = run_suite(
    args.output / "workspaces",
    execution_backend=args.execution_backend,
    docker_image=args.docker_image,
)
(args.output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(
    json.dumps(
        {
            "passed": result["passed"],
            "cases": len(result["cases"]),
            "real_model": False,
            "result": str(args.output / "result.json"),
        }
    )
)
raise SystemExit(0 if result["passed"] else 1)
