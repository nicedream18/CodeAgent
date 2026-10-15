from __future__ import annotations

import json
import os
import secrets
import socket
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.subagent_tasks import (
    SubagentTaskManager,
    reset_daemon_context,
    set_daemon_context,
)


class ApprovalWaiter:
    def __init__(self, request: ApprovalRequest) -> None:
        self.request = request
        self.event = threading.Event()
        self.decision: ApprovalDecision | None = None


class RunRecord:
    def __init__(self, run_id: str, root: Path) -> None:
        self.run_id = run_id
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.events: list[dict[str, Any]] = []
        self.condition = threading.Condition()
        self.status = "running"
        self.waiters: dict[str, ApprovalWaiter] = {}
        self.subscribers = 0
        self._load_events()

    def _load_events(self) -> None:
        path = self.root / "events.jsonl"
        if not path.exists():
            return
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                self.events.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    def append(self, event: dict[str, Any]) -> int:
        with self.condition:
            seq = len(self.events) + 1
            item = {"run_id": self.run_id, "seq": seq, "event": event}
            self.events.append(item)
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            self.condition.notify_all()
            return seq


class AgentDaemon:
    def __init__(
        self, state_dir: Path, host: str = "127.0.0.1", port: int = 8765
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("MokioClaw daemon only binds to loopback interfaces")
        self.state_dir = state_dir.expanduser()
        self.runs_dir = self.state_dir / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.subagent_manager = SubagentTaskManager(self.runs_dir)
        self.host, self.port = host, port
        self.token_path = self.state_dir / "auth.token"
        if self.token_path.exists():
            self.token = self.token_path.read_text(encoding="utf-8").strip()
        else:
            self.token = secrets.token_urlsafe(32)
            self.token_path.write_text(self.token + "\n", encoding="utf-8")
            try:
                self.token_path.chmod(0o600)
            except OSError:
                pass
        self.runs: dict[str, RunRecord] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="mokioclaw-run"
        )
        self.server = None

    def run(self) -> None:
        self.subagent_manager.start()
        daemon = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self) -> None:
                line = self.rfile.readline(4 * 1024 * 1024)
                if not line:
                    return
                try:
                    request = json.loads(line)
                    response = daemon.dispatch(request, self.wfile)
                    if response is not None:
                        self.wfile.write(
                            json.dumps(
                                response, ensure_ascii=False, default=str
                            ).encode()
                            + b"\n"
                        )
                        self.wfile.flush()
                except Exception as exc:
                    self.wfile.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": None,
                                "error": {
                                    "code": -32603,
                                    "message": f"{type(exc).__name__}: {exc}",
                                },
                            },
                            ensure_ascii=False,
                        ).encode()
                        + b"\n"
                    )
                    self.wfile.flush()

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        with Server((self.host, self.port), Handler) as server:
            self.server = server
            self.port = server.server_address[1]
            self.state_dir.mkdir(parents=True, exist_ok=True)
            (self.state_dir / "address.json").write_text(
                json.dumps({"host": self.host, "port": self.port}), encoding="utf-8"
            )
            try:
                (self.state_dir / "pid").write_text(str(os.getpid()), encoding="utf-8")
            except OSError:
                pass
            try:
                server.serve_forever(poll_interval=0.25)
            finally:
                self.subagent_manager.close()
                self.executor.shutdown(wait=False, cancel_futures=False)
                for path in (self.state_dir / "pid", self.state_dir / "address.json"):
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def dispatch(self, request: dict[str, Any], output) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request_id, (int, str)):
            return _error(request_id, -32600, "invalid JSON-RPC request")
        params = (
            request.get("params") if isinstance(request.get("params"), dict) else {}
        )
        if not secrets.compare_digest(str(params.get("token", "")), self.token):
            return _error(request_id, -32001, "unauthorized local client")
        method = request.get("method")
        if method == "start_run":
            run_id = self.start_run(params)
            return _result(request_id, {"run_id": run_id})
        if method == "subscribe":
            return self.subscribe(request_id, params, output)
        if method == "resolve_approval":
            return _result(request_id, self.resolve_approval(params))
        if method == "subagent_get":
            result = self.subagent_manager.get(
                str(params.get("subagent_run_id", "")),
                wait_seconds=float(params.get("wait_seconds", 0) or 0),
            )
            return _result(request_id, result or {"error_type": "unknown_subagent_run"})
        if method == "subagent_cancel":
            return _result(
                request_id,
                self.subagent_manager.cancel(str(params.get("subagent_run_id", ""))),
            )
        if method == "subagent_events":
            run_id = str(params.get("subagent_run_id", ""))
            result = self.subagent_manager.get(run_id)
            if result is None:
                return _error(request_id, -32004, f"unknown Subagent run: {run_id}")
            return _result(
                request_id,
                {
                    "task": result,
                    "events": self.subagent_manager.events(
                        run_id, int(params.get("after_seq", 0) or 0)
                    ),
                },
            )
        if method == "subagent_list":
            parent_run_id = str(params.get("parent_run_id", ""))
            return _result(
                request_id, self.subagent_manager.list_for_parent(parent_run_id)
            )
        if method == "status":
            return _result(
                request_id,
                {
                    "pid": os.getpid(),
                    "runs": {key: run.status for key, run in self.runs.items()},
                },
            )
        if method == "shutdown":
            self.subagent_manager.stop_accepting()
            if self.server is not None:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return _result(request_id, {"stopping": True})
        return _error(request_id, -32601, f"method not found: {method}")

    def start_run(self, params: dict[str, Any]) -> str:
        mode = str(params.get("mode", "run"))
        options = (
            params.get("options") if isinstance(params.get("options"), dict) else {}
        )
        run_id = f"run-{uuid4().hex[:12]}"
        record = RunRecord(run_id, self.runs_dir / run_id)
        with self.lock:
            self.runs[run_id] = record
        (record.root / "run.json").write_text(
            json.dumps(
                {"mode": mode, "options": options, "status": "running"}, indent=2
            ),
            encoding="utf-8",
        )
        self.executor.submit(self._run_task, record, mode, options)
        return run_id

    def _run_task(self, record: RunRecord, mode: str, options: dict[str, Any]) -> None:
        os.environ["MOKIO_DAEMON_WORKER"] = "1"
        context_token = set_daemon_context(
            self.subagent_manager, record.run_id, record.append
        )

        def approval_handler(request: ApprovalRequest) -> ApprovalDecision:
            waiter = ApprovalWaiter(request)
            record.waiters[request.id] = waiter
            record.append(
                {"type": "daemon_approval_request", "request": asdict(request)}
            )
            try:
                approval_timeout_seconds = max(
                    1.0, float(os.getenv("MOKIO_APPROVAL_TIMEOUT_SECONDS", "300"))
                )
            except ValueError:
                approval_timeout_seconds = 300.0
            if not waiter.event.wait(timeout=approval_timeout_seconds):
                record.waiters.pop(request.id, None)
                return ApprovalDecision(False, "Approval request timed out.")
            record.waiters.pop(request.id, None)
            return waiter.decision or ApprovalDecision(
                False, "Approval was not resolved."
            )

        try:
            from mokioclaw.core.agent import stream_agent_events, stream_session_events

            path_options = {
                key: Path(value) if value else None
                for key, value in options.items()
                if key in {"workspace", "resume_workspace", "session_workspace"}
            }
            common = {
                key: value
                for key, value in options.items()
                if key not in path_options and key != "task"
            }
            common["approval_handler"] = (
                approval_handler
                if common.get("approval_mode", "inline") == "inline"
                else None
            )
            stream = stream_session_events if mode == "session" else stream_agent_events
            for event in stream(options.get("task"), **common, **path_options):
                record.append(event)
            record.status = "finished"
        except BaseException as exc:
            record.append(
                {"type": "daemon_run_error", "error": f"{type(exc).__name__}: {exc}"}
            )
            record.status = "failed"
        finally:
            reset_daemon_context(context_token)
            try:
                metadata_path = record.root / "run.json"
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["status"] = record.status
                metadata_path.write_text(
                    json.dumps(payload, indent=2), encoding="utf-8"
                )
            except (OSError, json.JSONDecodeError):
                pass
            record.append({"type": "daemon_run_finished", "status": record.status})
            with record.condition:
                record.condition.notify_all()

    def subscribe(
        self, request_id: Any, params: dict[str, Any], output
    ) -> dict[str, Any] | None:
        run_id = str(params.get("run_id", ""))
        with self.lock:
            record = self.runs.get(run_id)
        if record is None:
            return _error(request_id, -32004, f"unknown run: {run_id}")
        with record.condition:
            record.subscribers += 1
        disconnected = False
        try:
            output.write(
                json.dumps(
                    _result(request_id, {"run_id": run_id, "status": record.status}),
                    ensure_ascii=False,
                ).encode()
                + b"\n"
            )
            output.flush()
            cursor = max(0, int(params.get("after_seq", 0)))
            while True:
                with record.condition:
                    while cursor >= len(record.events) and record.status == "running":
                        record.condition.wait(timeout=20)
                        if cursor >= len(record.events):
                            # The heartbeat also detects a client that closed
                            # its socket while an approval prompt was pending.
                            output.write(b"\n")
                            output.flush()
                            return None
                    pending = record.events[cursor:]
                    status = record.status
                for item in pending:
                    output.write(
                        json.dumps(
                            {"jsonrpc": "2.0", "method": "event", "params": item},
                            ensure_ascii=False,
                            default=str,
                        ).encode()
                        + b"\n"
                    )
                    output.flush()
                    cursor = int(item["seq"])
                if status != "running" and cursor >= len(record.events):
                    output.write(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "method": "stream_end",
                                "params": {"run_id": run_id, "status": status},
                            },
                            ensure_ascii=False,
                        ).encode()
                        + b"\n"
                    )
                    output.flush()
                    return None
        except (BrokenPipeError, ConnectionError, OSError):
            disconnected = True
            return None
        finally:
            with record.condition:
                record.subscribers = max(0, record.subscribers - 1)
                if disconnected and record.subscribers == 0:
                    for waiter in record.waiters.values():
                        waiter.decision = ApprovalDecision(
                            False, "Approval client disconnected."
                        )
                        waiter.event.set()

    def resolve_approval(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.runs.get(str(params.get("run_id", "")))
        request_id = str(params.get("approval_id", ""))
        waiter = run.waiters.get(request_id) if run else None
        if waiter is None:
            return {"ok": False, "error": "approval request is no longer pending"}
        tool_call_id = str(params.get("tool_call_id", ""))
        if (
            tool_call_id
            and waiter.request.tool_call_id
            and tool_call_id != waiter.request.tool_call_id
        ):
            return {
                "ok": False,
                "error": "approval response does not match the pending tool call",
            }
        waiter.decision = ApprovalDecision(
            bool(params.get("approved")), str(params.get("reason", ""))
        )
        waiter.event.set()
        return {"ok": True}


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
