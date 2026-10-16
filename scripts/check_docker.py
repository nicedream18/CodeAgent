"""A missing engine/image is a failure, never a successful all-skipped run."""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from mokioclaw.core.execution_backend import doctor

result = doctor("docker", os.getenv("MOKIO_DOCKER_IMAGE", "mokioclaw-execution:0.2.0"))
result["checked_at"] = datetime.now(UTC).isoformat()
if result["ok"]:
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "tests/test_execution_backend.py",
            "tests/test_internship_eval.py",
        ],
        env={**os.environ, "MOKIO_TEST_DOCKER": "1"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    result.update(
        ok=completed.returncode == 0,
        pytest=completed.stdout[-16000:],
        stderr=completed.stderr[-4096:],
    )
print(json.dumps(result, indent=2))
raise SystemExit(0 if result["ok"] else 1)
