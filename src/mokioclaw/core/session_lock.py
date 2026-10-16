"""Cross-process workspace lease for session writers and offline compact."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4


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
            try:
                owner = json.loads(lock.read_text(encoding="utf-8"))
                os.kill(int(owner["pid"]), 0)
            except ProcessLookupError:
                lock.unlink(missing_ok=True)
                continue
            except (OSError, ValueError, KeyError):
                pass
            raise RuntimeError(
                "session_busy: workspace has an active session writer; use compact --run-id for a running task"
            ) from None
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
