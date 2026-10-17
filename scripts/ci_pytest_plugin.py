"""A small result receipt; never copies test bodies, trace payloads or environments."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

STARTED = time.monotonic()
NESTED = bool(os.getenv("PYTEST_CURRENT_TEST"))


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    destination = os.getenv("CI_PYTEST_REPORT")
    if not destination or NESTED:
        return
    terminal = session.config.pluginmanager.getplugin("terminalreporter")
    stats = terminal.stats if terminal else {}
    counts = {name: len(stats.get(name, [])) for name in (
        "passed", "failed", "skipped", "error", "xfailed", "xpassed", "deselected")}
    passed_ids = [str(r.nodeid)[:512] for r in stats.get("passed", [])[:2000]]
    value = {"schema_version": 1, "session_token": os.getenv("CI_REPORT_TOKEN"),
             "exit_code": int(exitstatus), "collected": session.testscollected,
             "counts": counts, "passed_nodeids": passed_ids,
             "nodeids_truncated": len(stats.get("passed", [])) > 2000,
             "seconds": round(time.monotonic() - STARTED, 3)}
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)
