"""Prepare/validate three v2 slices with real Docker and zero model requests."""

import argparse
import json
from pathlib import Path

from mokioclaw.core.session_lock import session_writer
from mokioclaw.evaluation.job_tasks import (
    TASK_IDS,
    job_cache_root,
    prepare_job_task,
    validate_job_task,
)

parser = argparse.ArgumentParser()
parser.add_argument("--cache", type=Path, default=job_cache_root())
args = parser.parse_args()
repository = Path(__file__).resolve().parents[1]
records = {}
with session_writer(args.cache):
    for name in TASK_IDS:
        task = prepare_job_task(name, args.cache, repository)
        record = validate_job_task(task, args.cache)
        records[name] = record
        print(json.dumps({"task": name, "valid": record["valid"], "visible_ok": record["visible"]["ok"],
                          "base": record["base"]["target_failed"], "reference": record["reference"]["solved"]}))
    (args.cache / "validation.json").write_text(json.dumps(records, indent=2) + "\n")
raise SystemExit(0 if all(r["valid"] for r in records.values()) else 1)
