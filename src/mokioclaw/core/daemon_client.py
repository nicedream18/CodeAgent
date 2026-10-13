from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest


def daemon_state_dir() -> Path:
    return Path(os.getenv("MOKIO_DAEMON_DIR", str(Path.home() / ".mokioclaw" / "daemon"))).expanduser()


def ensure_daemon(*, start: bool = True) -> dict[str, Any] | None:
    state_dir = daemon_state_dir()
    address = _read_address(state_dir)
    if address and _health(address, state_dir):
        return address
    if not start:
        return None
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = state_dir / "start.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            address = _read_address(state_dir)
            if address and _health(address, state_dir):
                return address
            try:
                if time.time() - lock_path.stat().st_mtime > 30:
                    lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            if not lock_path.exists():
                return ensure_daemon(start=True)
            time.sleep(0.1)
        raise RuntimeError(f"Timed out waiting for MokioClaw daemon startup lock: {lock_path}")
    os.close(lock_fd)
    host = "127.0.0.1"
    port = int(os.getenv("MOKIO_DAEMON_PORT", "8765"))
    log_path = state_dir / "daemon.log"
    try:
        with log_path.open("ab") as log_handle:
            subprocess.Popen(
                [sys.executable, "-m", "mokioclaw.daemon", "--state-dir", str(state_dir), "--host", host, "--port", str(port)],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
                close_fds=(os.name != "nt"),
            )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            address = _read_address(state_dir)
            if address and _health(address, state_dir):
                return address
            time.sleep(0.1)
        raise RuntimeError(f"MokioClaw daemon did not start; inspect {log_path}")
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


def daemon_status(*, start: bool = False) -> dict[str, Any] | None:
    address = ensure_daemon(start=start)
    if not address:
        return None
    return _request(address, "status", {})


def stop_daemon() -> bool:
    address = ensure_daemon(start=False)
    if not address:
        return False
    _request(address, "shutdown", {})
    return True


def run_via_daemon(
    mode: str,
    task: str | None,
    *,
    options: dict[str, Any],
    approval_handler=None,
) -> Iterator[dict[str, Any]]:
    address = ensure_daemon(start=True)
    serialized = {key: str(value) if isinstance(value, Path) else value for key, value in options.items()}
    serialized["task"] = task
    started = _request(address, "start_run", {"mode": mode, "options": serialized})
    run_id = started["run_id"]
    after_seq = 0
    while True:
        request_id = uuid4().hex
        try:
            with socket.create_connection((address["host"], int(address["port"])), timeout=5) as sock:
                stream = sock.makefile("rwb", buffering=0)
                _write_request(stream, "subscribe", {"run_id": run_id, "after_seq": after_seq}, request_id)
                while True:
                    line = stream.readline(4 * 1024 * 1024)
                    if not line:
                        break
                    if line == b"\n":
                        break
                    message = json.loads(line)
                    if message.get("id") == request_id:
                        if "error" in message:
                            raise RuntimeError(message["error"].get("message", "subscribe failed"))
                        continue
                    if message.get("method") == "event":
                        item = message.get("params", {})
                        after_seq = max(after_seq, int(item.get("seq", 0)))
                        event = item.get("event", {})
                        if isinstance(event, dict) and event.get("type") == "daemon_approval_request":
                            raw_request = event.get("request", {})
                            request = ApprovalRequest(**raw_request)
                            if approval_handler:
                                decision = approval_handler(request)
                                decision = decision if isinstance(decision, ApprovalDecision) else ApprovalDecision(bool(decision))
                            else:
                                decision = ApprovalDecision(False, "No approval handler is connected.")
                            _request(address, "resolve_approval", {
                                "run_id": run_id,
                                "approval_id": request.id,
                                "tool_call_id": request.tool_call_id,
                                "approved": decision.approved,
                                "reason": decision.reason,
                            })
                            continue
                        yield event
                    elif message.get("method") == "stream_end":
                        return
        except (OSError, json.JSONDecodeError, ConnectionError):
            time.sleep(0.2)
            refreshed = ensure_daemon(start=False)
            if not refreshed:
                raise RuntimeError("MokioClaw daemon disconnected while a task was running")
            address = refreshed


def _request(address: dict[str, Any], method: str, params: dict[str, Any]) -> dict[str, Any]:
    token_path = daemon_state_dir() / "auth.token"
    token = token_path.read_text(encoding="utf-8").strip()
    request_id = uuid4().hex
    params = {**params, "token": token}
    with socket.create_connection((address["host"], int(address["port"])), timeout=10) as sock:
        stream = sock.makefile("rwb", buffering=0)
        _write_request(stream, method, params, request_id)
        line = stream.readline(1024 * 1024)
    if not line:
        raise ConnectionError("daemon closed the request without a response")
    response = json.loads(line)
    if response.get("error"):
        raise RuntimeError(response["error"].get("message", "daemon request failed"))
    return response.get("result", {})


def _write_request(stream, method: str, params: dict[str, Any], request_id: str) -> None:
    request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {**params, "token": _token()}}
    stream.write(json.dumps(request, ensure_ascii=False).encode() + b"\n")


def _token() -> str:
    return (daemon_state_dir() / "auth.token").read_text(encoding="utf-8").strip()


def _read_address(state_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((state_dir / "address.json").read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("host") and value.get("port"):
            return value
    except (OSError, json.JSONDecodeError):
        return None
    return None


def _health(address: dict[str, Any], state_dir: Path) -> bool:
    try:
        _request(address, "status", {})
        return True
    except (OSError, RuntimeError, json.JSONDecodeError):
        return False
