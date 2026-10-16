"""Owned evaluation workers: recovery never signals a reused/foreign PID."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from mokioclaw.core.execution_backend import BackendUnavailable, docker_cli


def cleanup_owner(owner: str) -> list[str]:
    try:
        ids = docker_cli(
            [
                "ps",
                "-aq",
                "--filter",
                "label=mokioclaw.execution=true",
                "--filter",
                "label=mokioclaw.owner=" + owner,
            ]
        ).stdout.split()
        if ids:
            docker_cli(["rm", "--force", *ids])
        return ids
    except BackendUnavailable:
        return ["cleanup-unconfirmed"]


def command_for(pid: int) -> str:
    if pid <= 1:
        return ""
    path = Path("/proc") / str(pid) / "cmdline"
    try:
        if path.exists():
            return path.read_bytes().replace(b"\0", b" ").decode(errors="replace")
        result = subprocess.run(
            ["ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def stop_worker(directory: Path, owner: str) -> bool:
    # The coordinator may have died after generation, while independent grading
    # was active. That container has its own explicit owner suffix.
    cleanup_owner(owner + "-grade")
    record = directory / "worker-process.json"
    if not record.exists():
        cleanup_owner(owner)
        return False
    identity = json.loads(record.read_text())
    pid, token = int(identity["pid"]), str(identity["token"])
    if pid <= 1 or len(token) != 32 or not all(c in "0123456789abcdef" for c in token):
        raise ValueError("Invalid worker process identity")
    spec = str((directory / "spec.json").resolve())

    def matches() -> bool:
        command = command_for(pid)
        return "mokioclaw.evaluation.worker" in command and token in command and spec in command

    if not matches():
        cleanup_owner(owner)
        return False
    try:
        if os.getpgid(pid) != pid:
            raise ValueError("Worker process group does not match its recorded ownership")
        os.killpg(pid, signal.SIGTERM)
        cleanup_owner(owner)
        deadline = time.monotonic() + 5
        while matches() and time.monotonic() < deadline:
            time.sleep(0.05)
        if matches():
            os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    cleanup_owner(owner)
    return True


def watch_parent(control: Any, spec: dict[str, Any], path: Path) -> threading.Event:
    stopped = threading.Event()
    token = spec.get("worker_token")
    if not token:
        return stopped  # In-process offline fixtures retain the old interface.
    parent = int(spec["parent_pid"])
    # The runner records the PID/nonce immediately after Popen. Do not race it
    # with a second writer to the same atomic temporary file.
    if os.getppid() != parent:
        control.cancel()
        return stopped

    def monitor() -> None:
        while not stopped.wait(0.2):
            if os.getppid() != parent:
                control.cancel()
                cleanup_owner(str(spec["owner"]))
                return

    threading.Thread(target=monitor, name="eval-parent-watch", daemon=True).start()
    return stopped
