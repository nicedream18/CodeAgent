"""Versioned job-readiness task slices; never mutate the internship-v1 cache."""

from __future__ import annotations

import json
import shutil
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from mokioclaw.core.execution_backend import DockerBackend, ExecutionRequest, resolve_image
from mokioclaw.evaluation.catalog import TASKS
from mokioclaw.evaluation.grader import validate_task
from mokioclaw.evaluation.models import EvalTask
from mokioclaw.evaluation.prepare import ASSETS, copy_source, prepare_task, sha, tree_hash

SUITE = "job-readiness-v1"
TASK_IDS = ("M02-v2", "M07-v2", "M08-v2")
FIXTURES = ("examples/mcp/echo_server.py",)
VISIBLE_COMMAND = "PYTHONPATH=src:. python -m pytest -q tests/test_visible_basics.py"
VISIBLE_TESTS = '''import sys
from pathlib import Path

from typer.testing import CliRunner

from mokioclaw.cli.app import app
from mokioclaw.core.state import RuntimeState
from mokioclaw.tools.execution import execute_tool
from mokioclaw.tools.mcp_tools import build_mcp_tools
from mokioclaw.tools.registry import build_tools


def test_basic_file_read_and_search(tmp_path):
    (tmp_path / "example.txt").write_text("hello world\\n", encoding="utf-8")
    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto")
    tools = {t.name: t for t in build_tools(runtime)}
    assert tools["FileReadTool"].invoke({"file_path": "example.txt"})["ok"]
    assert tools["GrepTool"].invoke({"pattern": "hello"})["matches"]


def test_cli_help_needs_no_model():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0


def test_stdio_fixture_is_present_and_callable(tmp_path):
    project = Path(__file__).resolve().parents[1]
    runtime = RuntimeState(workspace=tmp_path, approval_mode="auto")
    runtime.mcp_servers = [{"name": "example", "command": sys.executable,
        "args": [str(project / "examples/mcp/echo_server.py")], "cwd": str(project)}]
    echo = next(t for t in build_mcp_tools(runtime) if t.name == "MCP_example_echo")
    result = execute_tool(runtime, echo, {"text": "hello"})
    assert result["ok"] and result["content"] == ["echo: hello"]
'''


def job_cache_root() -> Path:
    return Path.home() / ".cache/mokioclaw/eval" / SUITE


def _recipe(task: EvalTask) -> dict[str, Any]:
    return {
        "suite": SUITE,
        "task_sha256": sha(task.model_dump_json(indent=2).encode() + b"\n"),
        "visible_command": VISIBLE_COMMAND,
        "visible_tests_sha256": sha(VISIBLE_TESTS.encode()),
        "fixtures": list(FIXTURES),
        "visible_scope": "Three standalone baseline checks; original full suite is not exposed.",
        "excluded_visible_tests": "Host-shim assumptions and unrelated integration suites are not baseline checks for this Docker slice. Trusted scoring selections are frozen separately.",
        "hidden_material": "reference.patch, tests.patch and trusted-tests remain evaluator-only",
    }


def prepare_job_task(task_id: str, cache: Path, repository: Path) -> EvalTask:
    if task_id not in TASK_IDS:
        raise ValueError(f"Unknown job-readiness task: {task_id}")
    directory = cache / "tasks" / task_id
    manifest = directory / "task.json"
    if manifest.exists():
        task = EvalTask.model_validate_json(manifest.read_text())
        if (
            task.id != task_id
            or task.version != SUITE
            or json.loads((directory / "recipe.json").read_text()) != _recipe(task)
            or tree_hash(directory / "base") != task.source_sha256
            or tree_hash(directory / "trusted-tests") != task.trusted_tests_sha256
            or sha((directory / "reference.patch").read_bytes()) != task.reference_patch_sha256
            or sha((directory / "tests.patch").read_bytes()) != task.test_patch_sha256
        ):
            raise ValueError("Frozen v2 task changed; inspect it and use a new version/cache")
        resolve_image(task.image_digest)
        return task
    if directory.exists():
        raise ValueError("Partial v2 task exists; existing material is never overwritten")
    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="prepare-v2-", dir=cache) as temp:
        staging = Path(temp)
        original_id = task_id.removesuffix("-v2")
        task = prepare_task(original_id, staging, repository)
        prepared = staging / "tasks" / original_id
        base = prepared / "base"
        # The original suite removed examples indiscriminately. Restore only the
        # required non-answer fixture from the exact same frozen source archive.
        with tarfile.open(ASSETS / "baseline.tar.gz") as archive:
            for name in FIXTURES:
                handle = archive.extractfile(name)
                if handle is None:
                    raise ValueError("Missing baseline fixture: " + name)
                target = base / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(handle.read())
        shutil.rmtree(base / "tests")
        (base / "tests").mkdir()
        (base / "tests/test_visible_basics.py").write_text(VISIBLE_TESTS)
        requirement = TASKS[original_id]["requirement"] + (
            "\n\n本工作区是固定工程任务切片，不是原仓库的完整测试环境。"
            "可见基础检查命令：" + VISIBLE_COMMAND + "。"
            "该检查验证基础读写/搜索、CLI 帮助和本地 stdio fixture；目标边界请自行验证。"
            "隐藏目标及固定回归由外部评分器在全新离线容器重建。"
            "只修改 src/mokioclaw 下实现，保留测试和示例 fixture。"
        )
        (base / "README.md").write_text("# MokioAgent task slice\n\n" + requirement + "\n")
        task = EvalTask.model_validate(
            {
                **task.model_dump(),
                "id": task_id,
                "version": SUITE,
                "source": task.source + "; v2 fixture and visible-scope repair",
                "requirement": requirement,
                "source_sha256": tree_hash(base),
            }
        )
        (prepared / "task.json").write_text(task.model_dump_json(indent=2) + "\n")
        (prepared / "recipe.json").write_text(json.dumps(_recipe(task), indent=2) + "\n")
        directory.parent.mkdir(parents=True, exist_ok=True)
        prepared.rename(directory)
    return task


def validate_job_task(task: EvalTask, cache: Path) -> dict[str, Any]:
    directory = cache / "tasks" / task.id
    report = cache / "validation" / task.id
    report.mkdir(parents=True, exist_ok=True)
    # Visible tests run on the unfixed source, without model credentials/network.
    with TemporaryDirectory(prefix="visible-") as temp:
        workspace = Path(temp) / "workspace"
        copy_source(directory / "base", workspace)
        visible = DockerBackend(task.image_digest).run(
            ExecutionRequest(workspace, VISIBLE_COMMAND, timeout_seconds=60, owner="v2-visible")
        )
    record = validate_task(task, directory, report)
    record["visible"] = visible.as_dict()
    record["valid"] = bool(record["valid"] and visible.ok and not visible.timed_out)
    (report / "validation.json").write_text(json.dumps(record, indent=2) + "\n")
    return record
