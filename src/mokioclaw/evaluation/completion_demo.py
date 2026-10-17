"""A small controlled real-model demo, separate from the frozen internship suite.

Only ``base/`` is copied into the Agent workspace. Reference and contract tests
remain in the evaluator cache and are used in a fresh grading container.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from mokioclaw.core.execution_backend import resolve_image
from mokioclaw.evaluation.models import EvalTask
from mokioclaw.evaluation.prepare import ASSETS, file_hashes, sha, tree_hash

SOURCE = """def take_batch(items, limit):
    \"\"\"Remove a FIFO batch from a mutable list.\"\"\"
    batch = []
    while items and len(batch) <= limit:
        batch.append(items.pop(0))
    return batch
"""

REFERENCE = """def take_batch(items, limit):
    \"\"\"Remove a FIFO batch from a mutable list.\"\"\"
    if limit < 0:
        raise ValueError("limit must be nonnegative")
    batch = []
    while items and len(batch) < limit:
        batch.append(items.pop(0))
    return batch
"""

VISIBLE_TESTS = """from batch_queue import take_batch


def test_empty_queue():
    items = []
    assert take_batch(items, 3) == []
    assert items == []


def test_drain_in_fifo_order():
    items = ["first", "second"]
    assert take_batch(items, 5) == ["first", "second"]
    assert items == []
"""

CONTRACT_TESTS = """import pytest

from batch_queue import take_batch


def test_exact_limit_and_remaining_queue():
    items = ["a", "b", "c", "d"]
    assert take_batch(items, 2) == ["a", "b"]
    assert items == ["c", "d"]


def test_zero_does_not_consume():
    items = ["a", "b"]
    assert take_batch(items, 0) == []
    assert items == ["a", "b"]


def test_negative_is_rejected_without_mutation():
    for items in ([], ["a", "b"]):
        original = items.copy()
        with pytest.raises(ValueError):
            take_batch(items, -1)
        assert items == original
"""

REQUIREMENT = """修复 src/batch_queue.py 的 take_batch(items, limit)。
items 是可变列表，limit 是整数。要求按 FIFO 顺序移除并返回至多 limit 项，
列表中剩余项的顺序不变；limit=0 时返回空列表且不消费；limit<0 时抛出
ValueError，不能改变原列表（包括空列表）。保留正常排空与空队列行为。
只修改实现，运行可见测试并自行检查这些边界，再汇报修改和实际验证结果。
可见测试命令：PYTHONPATH=src:. python -m pytest -q。
这是受控的小任务演示，不属于 internship-v1 的 12 任务或架构对照成绩。
"""


def _diff(before: str, after: str, name: str, *, new: bool = False) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="/dev/null" if new else "a/" + name,
            tofile="b/" + name,
        )
    )


def prepare_demo_task(cache: Path) -> EvalTask:
    """Materialize D01 once; reject changed bytes rather than repairing a cache."""
    directory = cache / "tasks" / "D01"
    base = directory / "base"
    files = {
        "src/batch_queue.py": SOURCE,
        "tests/test_queue.py": VISIBLE_TESTS,
        "README.md": "# FIFO batch queue\n\n" + REQUIREMENT,
    }
    reference = _diff(SOURCE, REFERENCE, "src/batch_queue.py")
    contract = _diff("", CONTRACT_TESTS, "tests/test_contract.py", new=True)
    task_file = directory / "task.json"
    if task_file.exists():
        task = EvalTask.model_validate_json(task_file.read_text())
        expected = {name: sha(text.encode()) for name, text in files.items()}
        if (
            file_hashes(base) != expected
            or tree_hash(base) != task.source_sha256
            or task.reference_patch_sha256 != sha(reference.encode())
            or task.test_patch_sha256 != sha(contract.encode())
            or (directory / "reference.patch").read_text() != reference
            or (directory / "tests.patch").read_text() != contract
            or task.requirement != REQUIREMENT
        ):
            raise ValueError("D01 frozen fixture changed; use a new version/cache")
        if resolve_image(task.image_digest) != task.image_digest:
            raise ValueError("Missing pinned demo image")
        return task
    if directory.exists():
        raise ValueError("Partial D01 cache exists; inspect it before preparing again")
    for name, text in files.items():
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    task = EvalTask(
        id="D01",
        version="completion-demo-v1",
        source="MokioAgent controlled FIFO queue demonstration; not a benchmark task",
        base_commit="controlled-fixture-" + tree_hash(base),
        source_sha256=tree_hash(base),
        dependency_sha256=sha((ASSETS / "requirements-execution-demo.txt").read_bytes()),
        image_digest=resolve_image("mokioclaw-execution:0.2.0"),
        requirement=REQUIREMENT,
        allowed_paths=["src/batch_queue.py"],
        target_tests=["tests/test_contract.py"],
        regression_tests=["tests/test_queue.py"],
        reference_patch_sha256=sha(reference.encode()),
        test_patch_sha256=sha(contract.encode()),
    )
    (directory / "reference.patch").write_text(reference)
    (directory / "tests.patch").write_text(contract)
    task_file.write_text(task.model_dump_json(indent=2) + "\n")
    return task
