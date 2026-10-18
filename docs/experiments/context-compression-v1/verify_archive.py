"""Verify archived A/B metrics without original workspaces, models, or Docker."""

from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from mokioclaw.evaluation.compression_metrics import measure_trial
from mokioclaw.evaluation.overhead import MAX_TRACE_BYTES


def verify(root: Path) -> dict[str, Any]:
    report = json.loads((root / "report.json").read_text())
    assert report["complete"] and report["measurement_valid"]
    assert report["recorded"] == report["planned"] == 18
    hashes = json.loads((root / "artifact-sha256.json").read_text())
    for name, expected in hashes.items():
        path = (root / name).resolve()
        assert path.is_relative_to(root.resolve())
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected, name
    verified = []
    for row in report["results"]:
        archived = root / row["arm"] / "trials" / row["trial_id"]
        with tempfile.TemporaryDirectory(prefix="compression-archive-verify-") as temporary:
            trial = Path(temporary)
            for name in ("result.json", "worker.json"):
                (trial / name).write_bytes((archived / name).read_bytes())
            with tarfile.open(archived / "trace.tar.gz") as archive:
                members = [
                    m for m in archive
                    if m.isfile() and m.name.startswith(".mokioclaw/traces/")
                    and m.name.endswith("/events.jsonl")
                ]
                assert len(members) == 1
                member = members[0]
                assert member.size <= MAX_TRACE_BYTES
                handle = archive.extractfile(member)
                assert handle is not None
                target = trial / "workspace/.mokioclaw/traces/archived/events.jsonl"
                target.parent.mkdir(parents=True)
                target.write_bytes(handle.read(MAX_TRACE_BYTES + 1))
            measured = measure_trial(trial, row["compression_enabled"])
            for key, value in measured.items():
                if key != "receipt":
                    assert row[key] == value, (row["arm"], row["trial_id"], key)
            assert measured["measurement_valid"]
        verified.append({"arm": row["arm"], "trial_id": row["trial_id"]})
    return {
        "ok": True, "frozen_runtime_commit": report["harness_revision"],
        "files_verified": len(hashes), "canonical_trials_verified": len(verified),
        "trials": verified, "original_cache_required": False,
        "real_model_calls": 0, "docker_calls": 0,
        "scope": "Hashes and canonical token/request/policy/stream metrics match frozen receipts. Does not regrade artifacts.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
