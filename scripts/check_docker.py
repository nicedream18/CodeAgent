"""Missing engines/images and skipped real tests fail, with bounded diagnostics."""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from ci_support import child_environment, run_command, write_json

from mokioclaw.core.execution_backend import BackendUnavailable, docker_cli, doctor

REQUIRED = {
    "tests/test_execution_backend.py::test_real_docker_isolation_timeout_and_paths",
    "tests/test_internship_eval.py::test_real_grader_rejects_noop_test_edits_and_fake_success",
}


def credible_tests(receipt: dict[str, Any], token: str) -> bool:
    return bool(receipt.get("session_token") == token and receipt.get("exit_code") == 0
                and REQUIRED.issubset(set(receipt.get("passed_nodeids", [])))
                and receipt.get("counts", {}).get("failed", 0) == 0
                and receipt.get("counts", {}).get("error", 0) == 0)


def check(output: Path) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    image = os.getenv("MOKIO_DOCKER_IMAGE", "mokioclaw-execution:0.2.0")
    result: dict[str, Any] = doctor("docker", image)
    result["checked_at"] = datetime.now(UTC).isoformat()
    if not result["ok"]:
        write_json(output / "result.json", result)
        return result
    owner, token = "ci-" + uuid4().hex, uuid4().hex
    with TemporaryDirectory(prefix="docker-check-home-") as temporary:
        environment = child_environment(Path(temporary), repository=Path(__file__).resolve().parents[1])
        environment.update(MOKIO_TEST_DOCKER="1", MOKIO_TEST_OWNER=owner,
                           MOKIO_DOCKER_IMAGE=result["image_digest"],
                           CI_PYTEST_REPORT=str((output / "pytest-summary.json").resolve()),
                           CI_REPORT_TOKEN=token, PYTEST_PLUGINS="ci_pytest_plugin")
        try:
            command = run_command([sys.executable, "-m", "pytest", "-q",
                                   "tests/test_execution_backend.py", "tests/test_internship_eval.py"],
                                  cwd=Path(__file__).resolve().parents[1], environment=environment,
                                  log=output / "pytest.log", timeout=120)
            receipt_path = output / "pytest-summary.json"
            receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
            result.update(ok=command["status"] == "passed" and credible_tests(receipt, token),
                          command=command, pytest=receipt, owner=owner)
        finally:
            try:
                ids = docker_cli(["ps", "-aq", "--filter", "label=mokioclaw.execution=true",
                                  "--filter", "label=mokioclaw.owner=" + owner]).stdout.split()
                if ids:
                    docker_cli(["rm", "--force", *ids])
                remaining = docker_cli(["ps", "-aq", "--filter",
                                        "label=mokioclaw.owner=" + owner]).stdout.strip()
                result["cleanup"] = {"removed": ids, "confirmed": not remaining}
                result["ok"] = bool(result.get("ok") and not remaining)
            except BackendUnavailable:
                result.update(ok=False, cleanup={"confirmed": False})
    write_json(output / "result.json", result)
    return result


def main() -> None:
    destination = os.getenv("MOKIO_DOCKER_REPORT")
    if destination:
        result = check(Path(destination))
    else:
        with TemporaryDirectory(prefix="mokio-docker-report-") as temporary:
            result = check(Path(temporary))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
