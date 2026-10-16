"""Generate/check the evaluation contract without calling models or Docker."""

import argparse
import json
from pathlib import Path

from mokioclaw.evaluation.catalog import CORE, TASKS, schedule
from mokioclaw.evaluation.models import EvalTask, EvalTrialConfig, EvalTrialResult, Grade

parser = argparse.ArgumentParser()
parser.add_argument("--check", action="store_true")
args = parser.parse_args()
record = {
    "schema_version": 1,
    "models": {
        cls.__name__: cls.model_json_schema()
        for cls in [EvalTask, EvalTrialConfig, EvalTrialResult, Grade]
    },
    "suite": {
        "name": "internship-v1",
        "core": list(CORE),
        "core_trials": len(schedule("core")),
        "coverage_trials": len(schedule("coverage")),
        "tasks": {
            name: {
                key: value
                for key, value in item.items()
                if key in {"requirement", "instance", "tests", "regression"}
            }
            for name, item in TASKS.items()
        },
    },
}
content = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
path = Path(__file__).resolve().parents[1] / "docs/EVAL_SCHEMA.json"
if args.check:
    if not path.exists() or path.read_text() != content:
        raise SystemExit("Evaluation documentation is stale: uv run python scripts/gen_eval_doc.py")
else:
    path.write_text(content)
