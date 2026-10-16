"""Rebuild trusted tests; accept source changes, never candidate test configuration."""

from __future__ import annotations

import json
import shlex
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from tempfile import TemporaryDirectory

from mokioclaw.core.execution_backend import BackendUnavailable, DockerBackend, ExecutionRequest
from mokioclaw.evaluation.models import EvalTask, Grade
from mokioclaw.evaluation.prepare import apply_patch, copy_source, file_hashes, sha

IGNORED_OUTPUTS = {"TODO.md", "NOTEPAD.md", "HISTORY_SUMMARY.md"}


def changed_paths(base: Path, candidate: Path) -> list[str]:
    original, current = file_hashes(base), file_hashes(candidate)
    return sorted(p for p in original.keys() | current.keys() if original.get(p) != current.get(p))


def _counts(path: Path) -> tuple[int, int, int, int]:
    if not path.is_file():
        return 0, 0, 0, 0
    try:
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
        failed = sum(c.find("failure") is not None for c in cases)
        errors = sum(c.find("error") is not None for c in cases)
        skipped = sum(c.find("skipped") is not None for c in cases)
        return len(cases), failed, errors, skipped
    except ET.ParseError:
        return 0, 0, 0, 0


def grade_candidate(
    task: EvalTask,
    directory: Path,
    candidate: Path,
    report_root: Path,
    *,
    seconds: int = 120,
    owner: str = "grading",
) -> Grade:
    started = time.monotonic()
    report_root.mkdir(parents=True, exist_ok=True)
    base = directory / "base"
    try:
        changes = changed_paths(base, candidate)
    except ValueError as exc:
        return Grade(out_of_scope=[str(exc)], report=str(report_root))

    def allowed(name: str) -> bool:
        return any(Path(name).full_match(pattern) for pattern in task.allowed_paths)

    out_of_scope = [p for p in changes if not allowed(p) and p not in IGNORED_OUTPUTS]
    test_changes = [
        p
        for p in out_of_scope
        if p.startswith("tests/")
        or Path(p).name in {"pytest.ini", "conftest.py", "pyproject.toml", "setup.cfg", "tox.ini"}
    ]
    try:
        with TemporaryDirectory(prefix="mokio-grade-", dir=report_root) as temp:
            staging = Path(temp) / "workspace"
            copy_source(base, staging)
            for name in changes:
                if not allowed(name):
                    continue
                source, destination = candidate / name, staging / name
                if source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                else:
                    destination.unlink(missing_ok=True)
            if task.id.startswith("M"):
                shutil.rmtree(staging / "tests")
                copy_source(directory / "trusted-tests", staging / "tests")
            else:
                patch = (directory / "tests.patch").read_text()
                if sha(patch.encode()) != task.test_patch_sha256:
                    raise ValueError("Trusted test patch changed")
                apply_patch(staging, patch)
            trusted_hashes = {
                p: h for p, h in file_hashes(staging).items() if p.startswith("tests/")
            }
            # -c prevents candidate pytest configuration from deciding which tests run.
            config = staging / ".eval-pytest.ini"
            config.write_text("[pytest]\naddopts =\n")
            commands = []
            for label, tests in [
                ("target", task.target_tests),
                ("regression", task.regression_tests),
            ]:
                selection = " ".join(shlex.quote(t) for t in tests)
                if label == "regression":
                    selection += " " + " ".join(
                        "--deselect=" + shlex.quote(t) for t in task.target_tests
                    )
                commands.append(
                    f"PYTHONPATH=src:. python -m pytest -q -c .eval-pytest.ini {selection} --junitxml=.{label}.xml; echo $? > .{label}.exit"
                )
            result = DockerBackend(task.image_digest).run(
                ExecutionRequest(
                    staging,
                    "\n".join(commands),
                    timeout_seconds=seconds,
                    max_output_chars=16000,
                    owner=owner,
                )
            )
            for key in ("stdout_full_output_path", "stderr_full_output_path"):
                reference = result.metadata.get(key)
                if reference and (staging / reference).is_file():
                    destination = report_root / Path(reference).name
                    shutil.copy2(staging / reference, destination)
                    result.metadata[key] = destination.name
            (report_root / "execution.json").write_text(
                json.dumps(result.as_dict(), indent=2) + "\n"
            )
            for label in ("target", "regression"):
                xml = staging / f".{label}.xml"
                if xml.exists():
                    shutil.copy2(xml, report_root / (label + ".xml"))
            target, tfail, terr, tskip = _counts(staging / ".target.xml")
            regression, rfail, rerr, rskip = _counts(staging / ".regression.xml")
            tested_hashes = {
                p: h for p, h in file_hashes(staging).items() if p.startswith("tests/")
            }
            if trusted_hashes != tested_hashes:
                test_changes.append("trusted-tests-modified-during-grading")
            target_exit = (
                (staging / ".target.exit").read_text().strip()
                if (staging / ".target.exit").exists()
                else "missing"
            )
            regression_exit = (
                (staging / ".regression.exit").read_text().strip()
                if (staging / ".regression.exit").exists()
                else "missing"
            )
            valid = target > 0 and regression > 0 and not result.timed_out and not terr and not rerr
            solved = bool(
                valid
                and not out_of_scope
                and not test_changes
                and tfail == rfail == tskip == rskip == 0
                and target_exit == regression_exit == "0"
            )
            return Grade(
                valid=valid,
                solved=solved,
                tests_run=target + regression,
                target_passed=target - tfail - terr - tskip,
                target_failed=tfail + terr + tskip,
                regression_failed=rfail + rerr + rskip,
                out_of_scope=out_of_scope,
                tampered_tests=test_changes,
                seconds=round(time.monotonic() - started, 3),
                report=str(report_root),
            )
    except (BackendUnavailable, OSError, ValueError) as exc:
        return Grade(
            infrastructure_error=f"{type(exc).__name__}: {exc}",
            out_of_scope=out_of_scope,
            tampered_tests=test_changes,
            seconds=round(time.monotonic() - started, 3),
            report=str(report_root),
        )


def validate_task(task: EvalTask, directory: Path, report_root: Path) -> dict[str, object]:
    baseline = grade_candidate(task, directory, directory / "base", report_root / "base")
    with TemporaryDirectory(prefix="mokio-gold-") as temp:
        gold = Path(temp) / "workspace"
        copy_source(directory / "base", gold)
        patch = (directory / "reference.patch").read_text()
        if sha(patch.encode()) != task.reference_patch_sha256:
            raise ValueError("Reference patch changed")
        apply_patch(gold, patch)
        # Upstream reference patches include docs. Only source is scored.
        for name in changed_paths(directory / "base", gold):
            if not any(Path(name).full_match(p) for p in task.allowed_paths):
                original = directory / "base" / name
                if original.exists():
                    shutil.copy2(original, gold / name)
                else:
                    (gold / name).unlink(missing_ok=True)
        reference = grade_candidate(task, directory, gold, report_root / "reference")
    valid = (
        baseline.target_failed > 0
        and not baseline.solved
        and reference.valid
        and reference.solved
        and not reference.regression_failed
    )
    record = {"valid": valid, "base": baseline.model_dump(), "reference": reference.model_dump()}
    report_root.mkdir(parents=True, exist_ok=True)
    (report_root / "validation.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
