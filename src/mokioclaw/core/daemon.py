from __future__ import annotations

import json
import os
import secrets
import socketserver
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any
from uuid import uuid4

from mokioclaw.core.approval import ApprovalDecision, ApprovalRequest
from mokioclaw.core.context_control import CURRENT_CONTROL, ContextControl
from mokioclaw.core.protocol import CAPABILITIES
from mokioclaw.core.run_control import CURRENT_RUN, RunControl, RunInterrupted
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
        self.context_control = ContextControl()
        self.run_control = RunControl()
        from mokioclaw.core.run_inbox import RunInbox

        self.run_control.inbox = RunInbox(self.root / "messages.json", self.condition)
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
            if event.get("type") == "custom_event" and isinstance(event.get("event"), dict):
                from mokioclaw.core.protocol import validate_event

                event = {**event, "event": validate_event(event["event"])}
                event = {
                    **event,
                    "event": {
                        "event_id": uuid4().hex,
                        "run_id": self.run_id,
                        "context_version": getattr(
                            getattr(self.run_control, "runtime", None),
                            "context_version",
                            0,
                        ),
                        **event["event"],
                    },
                }
            seq = len(self.events) + 1
            item = {"run_id": self.run_id, "seq": seq, "event": event}
            self.events.append(item)
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")
            self.condition.notify_all()
            return seq


class AgentDaemon:
    def __init__(self, state_dir: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
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
        self.approval_sessions = {}
        self.runs: dict[str, RunRecord] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mokioclaw-run")
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
                            json.dumps(response, ensure_ascii=False, default=str).encode() + b"\n"
                        )
                        self.wfile.flush()
                except json.JSONDecodeError:
                    self.wfile.write(
                        json.dumps(_error(None, -32700, "invalid JSON")).encode() + b"\n"
                    )
                    self.wfile.flush()
                except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
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
        from mokioclaw.core.transport_trace import transport_record

        transport_record(self.state_dir, "CLIENT→CORE", "rpc_received", request=request)
        result = self._dispatch_contract(request, output)
        transport_record(
            self.state_dir, "CORE→CLIENT", "rpc_response", request=request, response=result
        )
        return result

    def _dispatch_contract(self, request: dict[str, Any], output) -> dict[str, Any] | None:
        from pydantic import ValidationError

        from mokioclaw.core.protocol import PARAMS, RpcRequest

        try:
            parsed = RpcRequest.model_validate(request)
        except ValidationError:
            return _error(
                request.get("id") if isinstance(request, dict) else None,
                -32600,
                "invalid JSON-RPC request",
            )
        if not secrets.compare_digest(str(parsed.params.get("token", "")), self.token):
            return _error(parsed.id, -32001, "unauthorized local client")
        if parsed.method not in PARAMS:
            return _error(parsed.id, -32601, f"method not found: {parsed.method}")
        try:
            params = PARAMS[parsed.method].model_validate(parsed.params)
        except ValidationError as exc:
            return _error(parsed.id, -32602, str(exc.errors(include_input=False)))
        from mokioclaw.core.transport_trace import transport_record

        transport_record(self.state_dir, "CLIENT→CORE", "rpc_validated", request=request)
        return self._dispatch_validated(
            {**request, "params": params.model_dump(exclude_unset=True)}, output
        )

    def _dispatch_validated(self, request: dict[str, Any], output) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request_id, (int, str)):
            return _error(request_id, -32600, "invalid JSON-RPC request")
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        if not secrets.compare_digest(str(params.get("token", "")), self.token):
            return _error(request_id, -32001, "unauthorized local client")
        method = request.get("method")
        if method == "submit_run_message":
            with self.lock:
                record = self.runs.get(str(params.get("run_id", "")))
            if record is None:
                return _result(request_id, {"ok": False, "error_type": "unknown_run"})
            with record.condition:
                if any(
                    m["message_id"] == params.get("message_id")
                    for m in record.run_control.inbox.items
                ):
                    return _result(
                        request_id,
                        {
                            "run_id": record.run_id,
                            **record.run_control.inbox.submit(
                                params.get("message_id"), params.get("text")
                            ),
                        },
                    )
                if record.status != "running" or record.run_control.cancel_event.is_set():
                    return _result(request_id, {"ok": False, "error_type": "run_finished"})
                before = len(record.run_control.inbox.items)
                result = record.run_control.inbox.submit(
                    params.get("message_id"), params.get("text")
                )
                if result.get("ok") and len(record.run_control.inbox.items) > before:
                    record.append(
                        {
                            "type": "custom_event",
                            "event": {"type": "user_message_received", **result},
                        }
                    )
                    for waiter in record.waiters.values():
                        waiter.decision = ApprovalDecision(False, "Superseded by a user correction")
                        waiter.event.set()
                    record.waiters.clear()
                    record.append(
                        {
                            "type": "custom_event",
                            "event": {"type": "approvals_superseded"},
                        }
                    )
                return _result(request_id, {"run_id": record.run_id, **result})
        if method == "permissions":
            from mokioclaw.core.approval_rules import ApprovalRules

            workspace = Path(params["workspace"]).resolve()
            rules = ApprovalRules(workspace, params.get("session_id", ""), self.approval_sessions)
            if params.get("action", "list") != "list":
                rules.revoke(params.get("rule_id"), clear=params.get("action") == "clear")
                with self.lock:
                    active_runs = list(self.runs.values())
                for active in active_runs:
                    runtime = getattr(active.run_control, "runtime", None)
                    if runtime is not None and runtime.workspace.resolve() == workspace:
                        with active.condition:
                            for waiter in active.waiters.values():
                                waiter.decision = ApprovalDecision(False, "Approval policy changed")
                                waiter.event.set()
                            active.waiters.clear()
                        active.append(
                            {"type": "custom_event", "event": {"type": "approvals_superseded"}}
                        )
            return _result(request_id, {"ok": True, **rules.list_rules()})
        if method == "cancel_run":
            with self.lock:
                record = self.runs.get(str(params.get("run_id", "")))
            if record is None:
                return _result(request_id, {"ok": False, "error_type": "unknown_run"})
            with record.condition:
                if record.status == "running" and record.run_control.cancel():
                    for waiter in record.waiters.values():
                        waiter.decision = ApprovalDecision(False, "Run cancelled by user.")
                        waiter.event.set()
                    if not any(
                        item["event"].get("type") == "run_cancel_requested"
                        for item in record.events
                    ):
                        record.append({"type": "run_cancel_requested", "run_id": record.run_id})
                return _result(
                    request_id,
                    {
                        "ok": True,
                        "run_id": record.run_id,
                        "status": record.run_control.status
                        if record.run_control.status != "running"
                        else record.status,
                        "cancel_requested": record.run_control.cancel_event.is_set(),
                    },
                )
        if method in {"compact_run", "compact_status", "compact_cancel"}:
            run_id = str(params.get("run_id", ""))
            with self.lock:
                record = self.runs.get(run_id)
            if record is None:
                return _result(request_id, {"ok": False, "error_type": "unknown_run"})
            if method == "compact_status":
                return _result(
                    request_id,
                    record.context_control.get(str(params.get("request_id", ""))),
                )
            if method == "compact_cancel":
                result = record.context_control.cancel(str(params.get("request_id", "")))
                record.append({"type": "context_compact_request", **result})
                return _result(request_id, result)
            if record.status != "running":
                return _result(
                    request_id,
                    {"ok": False, "status": "failed", "error_type": "run_finished"},
                )
            result = record.context_control.request()
            record.append({"type": "context_compact_request", **result})
            return _result(request_id, result)
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
            return _result(request_id, self.subagent_manager.list_for_parent(parent_run_id))
        if method == "status":
            return _result(
                request_id,
                {
                    "protocol_version": 1,
                    "capabilities": CAPABILITIES,
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
        options = params.get("options") if isinstance(params.get("options"), dict) else {}
        run_id = f"run-{uuid4().hex[:12]}"
        record = RunRecord(run_id, self.runs_dir / run_id)
        record.run_control.approval_sessions = self.approval_sessions
        record.append({"type": "custom_event", "event": {"type": "run_started", "run_id": run_id}})
        with self.lock:
            self.runs[run_id] = record
        (record.root / "run.json").write_text(
            json.dumps({"mode": mode, "options": options, "status": "running"}, indent=2),
            encoding="utf-8",
        )
        self.executor.submit(self._run_task, record, mode, options)
        return run_id

    def _run_task(self, record: RunRecord, mode: str, options: dict[str, Any]) -> None:
        control_token = CURRENT_CONTROL.set(record.context_control)
        run_token = CURRENT_RUN.set(record.run_control)
        os.environ["MOKIO_DAEMON_WORKER"] = "1"
        context_token = set_daemon_context(self.subagent_manager, record.run_id, record.append)

        def approval_handler(request: ApprovalRequest) -> ApprovalDecision:
            waiter = ApprovalWaiter(request)
            with record.condition:
                record.run_control.check()
                record.waiters[request.id] = waiter
            record.append({"type": "daemon_approval_request", "request": asdict(request)})
            try:
                approval_timeout_seconds = max(
                    1.0, float(os.getenv("MOKIO_APPROVAL_TIMEOUT_SECONDS", "300"))
                )
            except ValueError:
                approval_timeout_seconds = 300.0
            if not waiter.event.wait(timeout=approval_timeout_seconds):
                record.waiters.pop(request.id, None)
                return ApprovalDecision(False, "Approval request timed out.")
            with record.condition:
                record.waiters.pop(request.id, None)
            record.run_control.check()
            from mokioclaw.core.run_inbox import check_redirect

            check_redirect(getattr(record.run_control, "runtime", None))
            return waiter.decision or ApprovalDecision(False, "Approval was not resolved.")

        terminal_status = "failed"
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
                approval_handler if common.get("approval_mode") in {None, "inline"} else None
            )
            stream = stream_session_events if mode == "session" else stream_agent_events
            for event in stream(options.get("task"), **common, **path_options):
                record.append(event)
            terminal_status = record.run_control.finish("finished")
        except (RunInterrupted, KeyboardInterrupt):
            record.run_control.cancel()
            terminal_status = record.run_control.finish("interrupted")
        except BaseException as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
            record.append({"type": "daemon_run_error", "error": f"{type(exc).__name__}: {exc}"})
            terminal_status = record.run_control.finish("failed")
        finally:
            record.run_control.close()
            for failed in record.context_control.close():
                record.append({"type": "context_compact_request", **failed})
            CURRENT_CONTROL.reset(control_token)
            CURRENT_RUN.reset(run_token)
            reset_daemon_context(context_token)
            try:
                metadata_path = record.root / "run.json"
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                payload["status"] = terminal_status
                metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass
            with record.condition:
                record.status = terminal_status
                record.append({"type": "daemon_run_finished", "status": terminal_status})
                record.condition.notify_all()

    def subscribe(self, request_id: Any, params: dict[str, Any], output) -> dict[str, Any] | None:
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
                    if (
                        "approval_memory" not in params.get("client_capabilities", [])
                        and item.get("event", {}).get("type") == "daemon_approval_request"
                    ):
                        original = item["event"]["request"]
                        item = {
                            **item,
                            "event": {
                                **item["event"],
                                "request": {
                                    k: v
                                    for k, v in original.items()
                                    if k
                                    in {"id", "command", "risk_reason", "tool_name", "tool_call_id"}
                                },
                            },
                        }
                    output.write(
                        json.dumps(
                            {"jsonrpc": "2.0", "method": "event", "params": item},
                            ensure_ascii=False,
                            default=str,
                        ).encode()
                        + b"\n"
                    )
                    output.flush()
                    from mokioclaw.core.transport_trace import transport_record

                    transport_record(
                        self.state_dir,
                        "CORE→CLIENT",
                        "event_push",
                        request={"id": request_id, "method": "subscribe"},
                        run_id=run_id,
                        event=item,
                    )
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
                        waiter.decision = ApprovalDecision(False, "Approval client disconnected.")
                        waiter.event.set()

    def resolve_approval(self, params: dict[str, Any]) -> dict[str, Any]:
        run = self.runs.get(str(params.get("run_id", "")))
        if run is None:
            return {"ok": False, "error": "unknown run"}
        with run.condition:
            request_id = str(params.get("approval_id", ""))
            waiter = run.waiters.get(request_id) if run else None
            if waiter is None or waiter.event.is_set():
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
            runtime = getattr(run.run_control, "runtime", None)
            from mokioclaw.core.approval_rules import runtime_rules

            if runtime is not None and (
                waiter.request.context_version != runtime.context_version
                or waiter.request.policy_version != runtime_rules(runtime).version()
            ):
                waiter.decision = ApprovalDecision(False, "Approval request is stale.")
                waiter.event.set()
                run.append(
                    {
                        "type": "custom_event",
                        "event": {"type": "approvals_superseded", "approval_ids": [request_id]},
                    }
                )
                return {"ok": False, "error": "approval request is stale"}
            for key in ("context_version", "policy_version"):
                if params.get(key) is not None and params[key] != getattr(waiter.request, key):
                    return {"ok": False, "error": "approval version mismatch"}
            waiter.decision = ApprovalDecision(
                bool(params.get("approved")),
                str(params.get("reason", "")),
                params.get("scope", "once"),
                params.get("match_scope", "exact"),
            )
            waiter.event.set()
            return {"ok": True}


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    if not isinstance(request_id, (str, int)) or isinstance(request_id, bool):
        request_id = None
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
