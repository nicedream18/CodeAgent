"""Cross-process workspace lease for session writers and offline compact."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

# A lock whose payload cannot be read may belong to a writer that just created the file
# and has not written it yet, so only reclaim it once it looks abandoned.
CORRUPT_LOCK_GRACE_SECONDS = 5.0

BUSY_MESSAGE = (
    "session_busy: workspace has an active session writer; use compact --run-id for a running task"
)


def _age_seconds(lock: Path) -> float | None:
    try:
        return time.time() - lock.stat().st_mtime
    except OSError:
        return None


def _is_stale(lock: Path) -> bool:
    """Return True when ``lock`` can be reclaimed without risking a live writer."""
    try:
        owner = json.loads(lock.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        age = _age_seconds(lock)
        return age is not None and age > CORRUPT_LOCK_GRACE_SECONDS

    try:
        pid = int(owner["pid"])
    except (KeyError, TypeError, ValueError):
        age = _age_seconds(lock)
        return age is not None and age > CORRUPT_LOCK_GRACE_SECONDS

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        # PermissionError means the process exists but belongs to another user.
        return False
    return False


@contextmanager
def session_writer(workspace: Path):
    lock = workspace / ".mokioclaw" / "session" / "writer.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if _is_stale(lock):
                lock.unlink(missing_ok=True)
                continue
            raise RuntimeError(BUSY_MESSAGE) from None
    else:
        raise RuntimeError("session_busy: could not acquire workspace session lease")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "identity": identity}, handle)
    try:
        yield
    finally:
        try:
            if json.loads(lock.read_text(encoding="utf-8")).get("identity") == identity:
                lock.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
