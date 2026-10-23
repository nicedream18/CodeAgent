from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mokioclaw.core.session_lock import (
    CORRUPT_LOCK_GRACE_SECONDS,
    session_writer,
)


def lock_path(workspace: Path) -> Path:
    return workspace / ".mokioclaw" / "session" / "writer.lock"


def write_lock(workspace: Path, payload: str, age_seconds: float = 0.0) -> Path:
    lock = lock_path(workspace)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(payload, encoding="utf-8")
    if age_seconds:
        old = time.time() - age_seconds
        os.utime(lock, (old, old))
    return lock


def test_acquires_and_releases_lock(tmp_path: Path) -> None:
    with session_writer(tmp_path):
        assert json.loads(lock_path(tmp_path).read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not lock_path(tmp_path).exists()


def test_reclaims_lock_of_dead_owner(tmp_path: Path) -> None:
    write_lock(tmp_path, json.dumps({"pid": 999_999_999, "identity": "gone"}))

    with session_writer(tmp_path):
        assert json.loads(lock_path(tmp_path).read_text(encoding="utf-8"))["identity"] != "gone"


def test_reclaims_old_corrupt_lock(tmp_path: Path) -> None:
    write_lock(tmp_path, "", age_seconds=CORRUPT_LOCK_GRACE_SECONDS + 1)

    with session_writer(tmp_path):
        assert lock_path(tmp_path).exists()


def test_keeps_fresh_corrupt_lock(tmp_path: Path) -> None:
    write_lock(tmp_path, "")

    with pytest.raises(RuntimeError, match="session_busy"):
        with session_writer(tmp_path):
            pass


def test_rejects_live_owner(tmp_path: Path) -> None:
    write_lock(tmp_path, json.dumps({"pid": os.getpid(), "identity": "live"}))

    with pytest.raises(RuntimeError, match="session_busy"):
        with session_writer(tmp_path):
            pass


def test_reclaims_lock_without_pid_after_grace(tmp_path: Path) -> None:
    write_lock(
        tmp_path,
        json.dumps({"identity": "no-pid"}),
        age_seconds=CORRUPT_LOCK_GRACE_SECONDS * 2,
    )

    with session_writer(tmp_path):
        assert json.loads(lock_path(tmp_path).read_text(encoding="utf-8"))["identity"] != "no-pid"


def test_keeps_fresh_lock_without_pid(tmp_path: Path) -> None:
    write_lock(tmp_path, json.dumps({"identity": "no-pid"}))

    with pytest.raises(RuntimeError, match="session_busy"):
        with session_writer(tmp_path):
            pass
