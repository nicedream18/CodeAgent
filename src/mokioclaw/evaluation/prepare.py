"""Online preparation, immutable cache and independent test material.

No model is constructed here. Candidate workspaces never contain this module,
reference patches, task locks, reports or target tests added by the benchmark.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path
from typing import Any

from mokioclaw.core.execution_backend import resolve_image
from mokioclaw.evaluation.catalog import BASE_COMMIT, DATASET_REVISION, TASKS
from mokioclaw.evaluation.models import EvalTask

ASSETS = Path(__file__).parent / "assets"
EXCLUDED = {".git", ".mokioclaw", ".venv", "__pycache__", ".pytest_cache", "node_modules"}


def cache_root() -> Path:
    return Path.home() / ".cache/mokioclaw/eval/internship-v1"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_hash(root: Path) -> str:
    return sha(json.dumps(file_hashes(root), sort_keys=True).encode())


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in EXCLUDED for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError(f"Symlink is not allowed in evaluation sources: {relative}")
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    digest.update(chunk)
            result[relative.as_posix()] = digest.hexdigest()
    return result


def copy_source(source: Path, target: Path) -> None:
    shutil.copytree(source, target, ignore=shutil.ignore_patterns(*EXCLUDED))


def apply_patch(root: Path, patch: str) -> None:
    if not patch.strip():
        return
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        input=patch,
        cwd=root,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError("Patch failed: " + result.stderr[-4096:])


def _unpack(data: bytes, target: Path, *, strip_first: bool) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        if not strip_first:
            archive.extractall(target, filter="data")
        else:
            for member in archive.getmembers():
                parts = Path(member.name).parts
                if len(parts) < 2:
                    continue
                member.name = Path(*parts[1:]).as_posix()
                archive.extract(member, target, filter="data")


def _gold_increment(task_id: str, root: Path) -> dict[str, str]:
    if task_id == "M07":
        path = "src/mokioclaw/tools/grep_tool.py"
        text = (root / path).read_text()
        text = text.replace(
            "head_limit=50, ignore_case=False):",
            "head_limit=50, ignore_case=False, fixed_strings=False):",
        )
        text = text.replace(
            "regex = re.compile(pattern,",
            "regex = re.compile(re.escape(pattern) if fixed_strings else pattern,",
        )
        text = text.replace(
            "                    if ignore_case:",
            "                    if fixed_strings:\n                        command.append('--fixed-strings')\n                    if ignore_case:",
        )
        registry = "src/mokioclaw/tools/registry.py"
        value = (root / registry).read_text()
        # The factory is intentionally changed as well as the Python function.
        value = value.replace(
            "head_limit=50, ignore_case=False:",
            "head_limit=50, ignore_case=False, fixed_strings=False:",
        )
        value = value.replace(
            "head_limit, ignore_case\n", "head_limit, ignore_case, fixed_strings\n"
        )
        return {path: text, registry: value}
    path = "src/mokioclaw/cli/app.py"
    text = (root / path).read_text()
    start = text.index("def trace_command(")
    prefix, rest = text[:start], text[start:]
    rest = rest.replace(
        "    raw: bool = False,", "    raw: bool = False,\n    since: str | None = None,", 1
    )
    rest = rest.replace(
        "    reader = TraceReader()",
        "    from mokioclaw.core.transport_trace import parse_since, record_since\n    try:\n        since_time = parse_since(since) if since is not None else None\n    except ValueError as exc:\n        raise typer.BadParameter(str(exc), param_hint='--since') from exc\n    reader = TraceReader()",
        1,
    )
    rest = rest.replace(
        '                payload = record.get("payload", {})',
        '                if since_time is not None and not record_since(record, since_time):\n                    continue\n                payload = record.get("payload", {})',
        1,
    )
    transport = "src/mokioclaw/core/transport_trace.py"
    value = (
        (root / transport).read_text()
        + """

def parse_since(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("--since requires an ISO8601 timestamp with timezone") from exc
    if parsed.tzinfo is None:
        raise ValueError("--since requires a timezone")
    return parsed.astimezone(UTC)


def record_since(record: dict[str, Any], since: datetime) -> bool:
    try:
        return parse_since(str(record.get("timestamp", ""))) >= since
    except ValueError:
        return False
"""
    )
    return {path: prefix + rest, transport: value}


def _make_patch(root: Path, replacements: dict[str, str]) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                (root / name).read_text().splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        for name, content in replacements.items()
    )


def prepare_task(task_id: str, cache: Path, repository: Path) -> EvalTask:
    definition = TASKS[task_id]
    directory = cache / "tasks" / task_id
    manifest = directory / "task.json"
    if manifest.exists():
        task = EvalTask.model_validate_json(manifest.read_text())
        if task.regression_tests != definition["regression"]:
            raise ValueError("Task recipe changed; prepare a new cache")
        if (
            sha((directory / "reference.patch").read_bytes()) != task.reference_patch_sha256
            or sha((directory / "tests.patch").read_bytes()) != task.test_patch_sha256
        ):
            raise ValueError("Cached patch changed")
        if (
            task.id.startswith("M")
            and tree_hash(directory / "trusted-tests") != task.trusted_tests_sha256
        ):
            raise ValueError("Cached trusted tests changed")
        if tree_hash(directory / "base") != task.source_sha256:
            raise ValueError(f"Corrupt cached task: {task_id}")
        if resolve_image(task.image_digest) != task.image_digest:
            raise ValueError(f"Missing pinned image for {task_id}")
        return task
    if directory.exists():
        shutil.rmtree(directory)
    base = directory / "base"
    directory.mkdir(parents=True)
    dataset_revision = None
    license_reference = None
    if task_id.startswith("M"):
        archive = (ASSETS / "baseline.tar.gz").read_bytes()
        _unpack(archive, base, strip_first=False)
        # Remove explanatory future answers, historical artifacts and private configuration.
        for name in ("docs", "examples", ".github", "项目篇规划.md"):
            path = base / name
            if path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        (base / "README.md").write_text(
            "# MokioAgent code task\nRun local tests with PYTHONPATH=src python -m pytest.\n"
        )
        base_commit = BASE_COMMIT
        if task_id in {"M07", "M08"}:
            reference_patch = _make_patch(base, _gold_increment(task_id, base))
        else:
            name = definition["file"]
            original = (base / name).read_text()
            if original.count(definition["old"]) != 1:
                raise ValueError(f"Mutation no longer applies uniquely: {task_id}")
            mutated = original.replace(definition["old"], definition["new"])
            (base / name).write_text(mutated)
            reference_patch = _make_patch(base, {name: original})
        image = resolve_image("mokioclaw-execution:0.2.0")
        dependency = sha((base / "uv.lock").read_bytes())
        requirement = definition["requirement"]
        allowed = ["src/mokioclaw/**"]
        targets, regression = definition["tests"], definition["regression"]
        test_patch = ""
        # Keep trusted base tests separately. Chosen target/regression modules are hidden.
        copy_source(base / "tests", directory / "trusted-tests")
        for hidden_file in (ASSETS / "hidden").glob("*.py.txt"):
            shutil.copy2(
                hidden_file, directory / "trusted-tests" / hidden_file.name.removesuffix(".txt")
            )
        hidden = {t.split("::")[0] for t in [*targets, *regression]}
        for name in hidden:
            (base / name).unlink(missing_ok=True)
    else:
        data = json.loads((ASSETS / "external.json").read_text())
        if data["revision"] != DATASET_REVISION:
            raise ValueError("Dataset revision changed")
        row = next(r for r in data["rows"] if r["instance_id"] == definition["instance"])
        base_commit = row["base_commit"]
        downloads = cache / "downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        archive_path = downloads / (base_commit + ".tar.gz")
        if not archive_path.exists():
            urllib.request.urlretrieve(
                f"https://codeload.github.com/{row['repo']}/tar.gz/{base_commit}", archive_path
            )
        if sha(archive_path.read_bytes()) != data["archives"][base_commit]:
            raise ValueError("Upstream source archive checksum mismatch")
        _unpack(archive_path.read_bytes(), base, strip_first=True)
        reference_patch, test_patch = row["patch"], row["test_patch"]
        requirement = row["problem_statement"]
        targets = json.loads(row["FAIL_TO_PASS"])
        regression = definition["regression"]
        dataset_revision = DATASET_REVISION
        allowed = ["requests/**"] if task_id == "E04" else ["src/flask/**"]
        image = resolve_image(
            {
                "E01": "mokioclaw-flask20:internship-v1",
                "E02": "mokioclaw-flask23:internship-v1",
                "E03": "mokioclaw-flask23:internship-v1",
                "E04": "mokioclaw-requests210:internship-v1",
            }[task_id]
        )
        dependency = sha(
            (
                ASSETS
                / (
                    "requirements-"
                    + (
                        "requests210"
                        if task_id == "E04"
                        else "flask20"
                        if task_id == "E01"
                        else "flask23"
                    )
                    + ".txt"
                )
            ).read_bytes()
        )
        license_path = next(p for p in base.iterdir() if p.name.upper().startswith("LICENSE"))
        license_reference = str(license_path.relative_to(base))
        (directory / "UPSTREAM-LICENSE").write_bytes(license_path.read_bytes())
    task = EvalTask(
        id=task_id,
        source=definition.get(
            "instance",
            "MokioAgent seeded defect"
            if task_id not in {"M07", "M08"}
            else "MokioAgent incremental requirement",
        ),
        base_commit=base_commit,
        source_sha256=tree_hash(base),
        dependency_sha256=dependency,
        image_digest=image,
        requirement=requirement,
        allowed_paths=allowed,
        target_tests=targets,
        regression_tests=regression,
        reference_patch_sha256=sha(reference_patch.encode()),
        test_patch_sha256=sha(test_patch.encode()),
        trusted_tests_sha256=tree_hash(directory / "trusted-tests")
        if task_id.startswith("M")
        else "",
        dataset_revision=dataset_revision,
        upstream_license=license_reference,
    )
    (directory / "reference.patch").write_text(reference_patch)
    (directory / "tests.patch").write_text(test_patch)
    manifest.write_text(task.model_dump_json(indent=2) + "\n")
    return task


def prepare_suite(cache: Path, repository: Path) -> dict[str, Any]:
    cache.mkdir(parents=True, exist_ok=True)
    records = {}
    for task_id in TASKS:
        try:
            task = prepare_task(task_id, cache, repository)
            records[task_id] = {
                "prepared": True,
                "manifest_sha256": sha(task.model_dump_json().encode()),
            }
        except Exception as exc:
            records[task_id] = {
                "prepared": False,
                "infrastructure_error": f"{type(exc).__name__}: {exc}",
            }
    (cache / "preparation.json").write_text(json.dumps(records, indent=2) + "\n")
    return records
