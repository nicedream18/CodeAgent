from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest


def daemon_state_dir() -> Path:
    return Path(
        os.getenv("MOKIO_DAEMON_DIR", str(Path.home() / ".mokioclaw" / "daemon"))
    ).expanduser()


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
        raise RuntimeError(
            f"Timed out waiting for MokioClaw daemon startup lock: {lock_path}"
        ) from None
    os.close(lock_fd)
    host = "127.0.0.1"
    port = int(os.getenv("MOKIO_DAEMON_PORT", "8765"))
    log_path = state_dir / "daemon.log"
    try:
        with log_path.open("ab") as log_handle:
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "mokioclaw.daemon",
                    "--state-dir",
                    str(state_dir),
                    "--host",
                    host,
                    "--port",
                    str(port),
                ],
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


def cancel_run(run_id: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(address, "cancel_run", {"run_id": run_id})


def submit_run_message(run_id: str, message_id: str, text: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(
        address,
        "submit_run_message",
        {"run_id": run_id, "message_id": message_id, "text": text},
    )


def compact_run(run_id: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(address, "compact_run", {"run_id": run_id})


def compact_status(run_id: str, request_id: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(address, "compact_status", {"run_id": run_id, "request_id": request_id})


def compact_cancel(run_id: str, request_id: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(address, "compact_cancel", {"run_id": run_id, "request_id": request_id})


def subagent_status(run_id: str, *, wait_seconds: float = 0) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(
        address,
        "subagent_get",
        {"subagent_run_id": run_id, "wait_seconds": min(30.0, max(0.0, wait_seconds))},
    )


def subagent_cancel(run_id: str) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(address, "subagent_cancel", {"subagent_run_id": run_id})


def subagent_events(run_id: str, *, after_seq: int = 0) -> dict[str, Any]:
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    return _request(
        address,
        "subagent_events",
        {"subagent_run_id": run_id, "after_seq": max(0, after_seq)},
    )


def iter_subagent_events(run_id: str, *, after_seq: int = 0) -> Iterator[dict[str, Any]]:
    cursor = max(0, after_seq)
    while True:
        snapshot = subagent_events(run_id, after_seq=cursor)
        for item in snapshot.get("events", []):
            cursor = max(cursor, int(item.get("seq", 0)))
            yield item
        task = snapshot.get("task", {})
        if task.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
            return
        time.sleep(0.5)


def run_via_daemon(
    mode: str,
    task: str | None,
    *,
    options: dict[str, Any],
    approval_handler=None,
) -> Iterator[dict[str, Any]]:
    address = ensure_daemon(start=True)
    serialized = {
        key: str(value) if isinstance(value, Path) else value for key, value in options.items()
    }
    if serialized.get("execution_backend") is None:
        serialized.pop("execution_backend", None)
    serialized["task"] = task
    if serialized.get("skill"):
        capabilities = _request(address, "status", {}).get("capabilities", [])
        if "explicit_skill" not in capabilities:
            raise RuntimeError(
                "Daemon does not support explicit_skill; update/restart it after active tasks finish"
            )
    if serialized.get("execution_backend") == "docker":
        capabilities = _request(address, "status", {}).get("capabilities", [])
        if "execution_backend" not in capabilities:
            raise RuntimeError("Daemon does not support Docker; update/restart it after active tasks finish")
    started = _request(address, "start_run", {"mode": mode, "options": serialized})
    run_id = started["run_id"]
    yield from iter_run_events(run_id, approval_handler=approval_handler)


def iter_run_events(run_id: str, *, approval_handler=None):
    """Attach to an existing run; reconnect without starting another run."""
    address = ensure_daemon(start=False)
    if not address:
        raise RuntimeError("MokioClaw daemon is stopped")
    after_seq = 0
    while True:
        request_id = uuid4().hex
        try:
            with socket.create_connection(
                (address["host"], int(address["port"])), timeout=5
            ) as sock:
                stream = sock.makefile("rwb", buffering=0)
                _write_request(
                    stream,
                    "subscribe",
                    {"run_id": run_id, "after_seq": after_seq},
                    request_id,
                )
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
                        if isinstance(event, dict):
                            event = {**event, "run_id": run_id}
                        if (
                            isinstance(event, dict)
                            and event.get("type") == "daemon_approval_request"
                        ):
                            raw_request = event.get("request", {})
                            request = ApprovalRequest(**raw_request)
                            if approval_handler:
                                decision = approval_handler(request)
                                decision = (
                                    decision
                                    if isinstance(decision, ApprovalDecision)
                                    else ApprovalDecision(bool(decision))
                                )
                            else:
                                decision = ApprovalDecision(
                                    False, "No approval handler is connected."
                                )
                            _request(
                                address,
                                "resolve_approval",
                                {
                                    "run_id": run_id,
                                    "approval_id": request.id,
                                    "tool_call_id": request.tool_call_id,
                                    "approved": decision.approved,
                                    "reason": decision.reason,
                                    "scope": decision.scope,
                                    "match_scope": decision.match_scope,
                                    "context_version": request.context_version,
                                    "policy_version": request.policy_version,
                                },
                            )
                            continue
                        yield event
                    elif message.get("method") == "stream_end":
                        return
        except KeyboardInterrupt:
            # SIGINT cancels explicitly; socket disconnects continue to reconnect.
            cancel_run(run_id)

            def approval_handler(_request):
                return ApprovalDecision(False, "Run cancelling.")
        except (OSError, json.JSONDecodeError, ConnectionError):
            time.sleep(0.2)
            refreshed = ensure_daemon(start=False)
            if not refreshed:
                raise RuntimeError(
                    "MokioClaw daemon disconnected while a task was running"
                ) from None
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
    from mokioclaw.core.protocol import RpcResponse

    response = RpcResponse.model_validate(json.loads(line)).model_dump(exclude_unset=True)
    if response.get("error"):
        raise RuntimeError(response["error"].get("message", "daemon request failed"))
    return response.get("result", {})


def _write_request(stream, method: str, params: dict[str, Any], request_id: str) -> None:
    request = {
        "jsonrpc": "2.0",
        "protocol_version": 1,
        "id": request_id,
        "method": method,
        "params": {
            **params,
            "token": _token(),
            "client_capabilities": ["approval_memory", "typed_protocol"],
        },
    }
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


def permissions(workspace: Path, *, action="list", rule_id=None, session_id=""):
    address = ensure_daemon(start=False)
    if address:
        capabilities = _request(address, "status", {}).get("capabilities", [])
        if "approval_memory" not in capabilities:
            raise RuntimeError("Daemon does not support approval_memory")
        return _request(
            address,
            "permissions",
            {
                "workspace": str(workspace.resolve()),
                "action": action,
                "rule_id": rule_id,
                "session_id": session_id,
            },
        )
    from mokioclaw.core.approval_rules import ApprovalRules

    rules = ApprovalRules(workspace, session_id)
    if action != "list":
        rules.revoke(rule_id, clear=action == "clear")
    return {"ok": True, **rules.list_rules()}
