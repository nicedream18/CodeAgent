"""Bounded, conservative collaboration-loop detection (no raw arguments retained)."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

Writer = Callable[[dict[str, Any]], Any] | None
_VOLATILE = frozenset(
    {
        "tool_call_id",
        "call_id",
        "run_id",
        "stream_id",
        "handoff_id",
        "event_id",
        "container_id",
        "container_name",
        "pid",
        "process_id",
        "duration_ms",
        "duration_seconds",
        "elapsed_ms",
        "elapsed_seconds",
        "timestamp",
        "stdout_path",
        "stdout_full_output_path",
        "stderr_full_output_path",
        "stderr_path",
        "full_output_path",
        "output_ref",
        "log_path",
        "retries",
        "retry_stop_reason",
        "backend",
        "estimated_tokens",
    }
)
_MESSAGES = {
    "repeated_success": "Reuse existing evidence; repeat reads/checks only to answer a new question or inspect changed files.",
    "repeated_failure": "The same call has failed three times. Change its arguments or strategy; do not repeat the unchanged failure.",
    "failed_call_loop": "An unchanged non-transient failure was requested a fourth time.",
    "repeated_sequence": "A two-to-four-call sequence repeated three times without new evidence or file changes.",
    "no_progress": "Six model rounds made no observable progress while calls or validation failures repeated.",
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode()
    ).hexdigest()


def stable_result(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): stable_result(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, (list, tuple)):
        return [stable_result(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"\b\d{4}-\d{2}-\d{2}T[\d:.+Z-]+", "<time>", value)
        value = re.sub(r"\b\d+(?:\.\d+)?\s*(?:seconds?|ms|s)\b", "<duration>", value)
    return value


@dataclass(frozen=True)
class LoopDecision:
    action: str
    reason: str
    call: str = ""


class LoopRecovery(BaseException):
    """Dedicated control signal: never a tool failure, retry or chat fallback."""

    def __init__(self, decision: LoopDecision, state: dict[str, Any] | None = None):
        self.decision = decision
        self.state = state or {}
        self.reason = "collaboration_loop_detected"
        super().__init__(decision.reason)


class LoopGuard:
    def __init__(self) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=128)
        self.evidence: deque[str] = deque(maxlen=128)
        self.scope = ""
        self.workspace = ""
        self.epoch = 0
        self.boundary = 0
        self.rounds = 0
        self.recoveries = 0
        self.warning = ""
        self.pending: LoopDecision | None = None
        self.verifications: deque[str] = deque(maxlen=8)

    def invalidate(self, scope: str) -> None:
        if scope != self.scope:
            self.scope = scope
            self.restart_window()
            self.evidence.clear()
            self.warning = ""
            self.pending = None

    def progress(self) -> None:
        self.epoch += 1
        self.rounds = 0
        self.pending = None

    def restart_window(self) -> None:
        self.boundary += 1
        self.verifications.clear()
        self.progress()

    def file_state(self, fingerprint: str) -> None:
        if self.workspace and self.workspace != fingerprint:
            self.restart_window()
        self.workspace = fingerprint

    def call_fingerprint(self, name: str, args: Mapping[str, Any], environment: Any) -> str:
        normalized = dict(args)
        if name == "BashTool" and isinstance(normalized.get("command"), str):
            normalized["command"] = normalized["command"].strip()
        return digest([name, normalized, environment])

    def before_call(self, call: str) -> LoopDecision | None:
        if self.pending:
            return self.pending
        recent = [item for item in self.history if item["epoch"] == self.epoch]
        # Consecutive results, ignoring internal retries (observed once by executor).
        if len(recent) >= 3 and all(
            item["call"] == call and item["failure"] for item in recent[-3:]
        ):
            if len({item.get("failure_kind", item["result"]) for item in recent[-3:]}) == 1:
                return LoopDecision("recover", "failed_call_loop", call)
        return None

    def observe(self, call: str, name: str, result: Any) -> LoopDecision | None:
        from mokioclaw.core.tool_output import bound_value

        normalized = stable_result(bound_value(result))
        outcome = digest(normalized)
        data = result if isinstance(result, dict) else {}
        exit_code = data.get("exit_code", 0)
        ok = data.get("ok", True) is not False and (
            exit_code is None or (type(exit_code) is int and exit_code == 0)
        )
        failure = not ok and data.get("error_type") != "transient" and not data.get("timed_out")
        # A novel read range/content or actual command outcome is evidence. TODOs,
        # handoff UUIDs, summaries and compression alone never count as progress.
        evidence = digest([call, outcome])
        if name in {"FileReadTool", "GrepTool", "FileGlobTool", "BashTool"}:
            has_evidence = name == "BashTool" or bool(
                data.get("content") or data.get("matches") or data.get("paths")
            )
            if (ok or name == "BashTool") and has_evidence and evidence not in self.evidence:
                self.evidence.append(evidence)
                self.progress()
        self.history.append(
            {
                "call": call,
                "result": outcome,
                "failure": bool(failure),
                "ok": ok,
                "epoch": self.epoch,
                "boundary": self.boundary,
                "failure_kind": digest(
                    [data.get("error_type", "tool_error"), data.get("exit_code")]
                ),
            }
        )
        recent = [item for item in self.history if item["epoch"] == self.epoch]
        same = [item for item in recent if item["call"] == call and item["result"] == outcome]
        if ok and len(same) == 3:
            self.warning = "repeated_success"
        if (
            failure
            and len(recent) >= 3
            and all(
                x["call"] == call
                and x["failure"]
                and x["failure_kind"] == recent[-1]["failure_kind"]
                for x in recent[-3:]
            )
        ):
            self.warning = "repeated_failure"
        # Success-only repeats of a single read are advisory. Sequences need at
        # least two distinct calls so A,A,A cannot become a synthetic A/B cycle.
        for width in range(2, 5):
            tail = list(self.history)[-3 * width :]
            if (
                len(tail) != 3 * width
                or len({item["call"] for item in tail}) < 2
                or any(item.get("boundary", 0) != self.boundary for item in tail)
                or any(item["epoch"] != self.epoch for item in tail[width:])
            ):
                continue
            parts = [
                [(x["call"], x["result"]) for x in tail[i : i + width]]
                for i in range(0, 3 * width, width)
            ]
            if parts[0] == parts[1] == parts[2]:
                self.pending = LoopDecision("recover", "repeated_sequence", call)
                break
        return self.pending

    def model_round(self) -> LoopDecision | None:
        if self.pending:
            return self.pending
        recent = [item for item in self.history if item["epoch"] == self.epoch]
        repeated = len({item["call"] for item in recent}) < len(recent)
        failed_checks = (
            len(self.verifications) >= 2 and self.verifications[-1] == self.verifications[-2]
        )
        if self.rounds >= 6 and (repeated or failed_checks):
            return LoopDecision("recover", "no_progress")
        self.rounds += 1
        return None

    def verification(self, checks: Any, passed: bool) -> None:
        outcome = digest(stable_result(checks))
        if checks and outcome not in self.verifications:
            self.progress()
        if not passed:
            self.verifications.append(outcome)

    def recover(self) -> bool:
        if self.recoveries >= 1:
            return False
        self.recoveries += 1
        self.restart_window()
        self.warning = "repeated_failure"
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "history": list(self.history),
            "evidence": list(self.evidence),
            "scope": self.scope,
            "workspace": self.workspace,
            "epoch": self.epoch,
            "boundary": self.boundary,
            "rounds": self.rounds,
            "recoveries": self.recoveries,
            "warning": self.warning,
            "verifications": list(self.verifications),
            "pending": self.pending.reason if self.pending else "",
        }

    @classmethod
    def restore(cls, payload: Any) -> LoopGuard:
        guard = cls()
        if not isinstance(payload, dict):
            return guard

        def hash_value(value: Any) -> str:
            return (
                value
                if isinstance(value, str)
                and len(value) == 64
                and all(c in "0123456789abcdef" for c in value)
                else ""
            )

        guard.scope, guard.workspace = (
            hash_value(payload.get("scope")),
            hash_value(payload.get("workspace")),
        )
        guard.epoch = max(0, int(payload.get("epoch", 0)))
        guard.boundary = max(0, int(payload.get("boundary", 0)))
        guard.rounds = min(6, max(0, int(payload.get("rounds", 0))))
        guard.recoveries = min(1, max(0, int(payload.get("recoveries", 0))))
        guard.warning = payload.get("warning", "") if payload.get("warning") in _MESSAGES else ""
        for item in payload.get("history", [])[-128:]:
            if (
                isinstance(item, dict)
                and hash_value(item.get("call"))
                and hash_value(item.get("result"))
            ):
                guard.history.append(
                    {
                        "call": item["call"],
                        "result": item["result"],
                        "epoch": int(item.get("epoch", 0)),
                        "boundary": int(item.get("boundary", 0)),
                        "ok": bool(item.get("ok")),
                        "failure": bool(item.get("failure")),
                        "failure_kind": hash_value(item.get("failure_kind")) or item["result"],
                    }
                )
        guard.evidence.extend(x for x in payload.get("evidence", [])[-128:] if hash_value(x))
        guard.verifications.extend(
            x for x in payload.get("verifications", [])[-8:] if hash_value(x)
        )
        if payload.get("pending") in _MESSAGES:
            guard.pending = LoopDecision("recover", payload["pending"])
        return guard


def guard_for(runtime: Any) -> LoopGuard:
    if runtime.loop_guard is None:
        runtime.loop_guard = LoopGuard()
    guard: LoopGuard = runtime.loop_guard
    return guard


def workspace_fingerprint(runtime: Any) -> str:
    """Streaming digest, honoring search scope; stores no file contents or paths."""
    from mokioclaw.tools.grep_tool import _iter_files

    h = hashlib.sha256()
    for path in _iter_files(runtime.workspace, workspace=runtime.workspace.resolve()):
        if path.name in {"TODO.md", "NOTEPAD.md", "HISTORY_SUMMARY.md"}:
            continue
        try:
            h.update(path.relative_to(runtime.workspace.resolve()).as_posix().encode())
            with path.open("rb") as handle:
                while chunk := handle.read(65536):
                    h.update(chunk)
        except OSError:
            continue
    return h.hexdigest()


def sync_guard(runtime: Any) -> LoopGuard:
    from mokioclaw.core.approval_rules import runtime_rules
    from mokioclaw.core.execution_backend import execution_fingerprint

    guard = guard_for(runtime)
    inbox = getattr(runtime.run_control, "inbox", None)
    corrections = (
        [item["message_id"] for item in inbox.items if item["status"] == "applied"] if inbox else []
    )
    guard.invalidate(
        digest(
            [
                corrections,
                runtime_rules(runtime).version(),
                execution_fingerprint(runtime),
                runtime.tool_policies,
                runtime.bash_deny_patterns,
                runtime.bash_allow_patterns,
                runtime.approval_mode,
            ]
        )
    )
    guard.file_state(workspace_fingerprint(runtime))
    return guard


def event(
    runtime: Any, action: str, reason: str, *, call_id: str = "", node: str = ""
) -> dict[str, Any]:
    active = runtime.active_subagent_budget
    return {
        "type": "loop_" + action,
        "event_id": uuid4().hex,
        "run_id": runtime.run_id,
        "stream_id": getattr(runtime, "loop_stream_id", ""),
        "tool_call_id": call_id or runtime.loop_tool_call_id,
        "handoff_id": active.handoff_id if active else "",
        "node": node,
        "context_version": runtime.context_version,
        "reason": reason,
        "message": _MESSAGES.get(reason, reason),
        "recoveries": guard_for(runtime).recoveries,
    }


def raise_decision(runtime: Any, decision: LoopDecision | None) -> None:
    if decision is None:
        return
    from mokioclaw.core.run_control import check_run
    from mokioclaw.core.run_inbox import check_redirect

    check_run(runtime)
    check_redirect(runtime)
    runtime.context_version += 1  # invalidates any outstanding single-call approval
    control = runtime.run_control
    if control is not None and getattr(control, "supersede_approvals", None):
        control.supersede_approvals()
    state = {
        k: copy.deepcopy(v)
        for k, v in (runtime.foreground_state or {}).items()
        if k not in {"runtime", "messages"}
    }
    raise LoopRecovery(decision, state)


def before_tool(runtime: Any, name: str, args: dict[str, Any]) -> str:
    from mokioclaw.core.execution_backend import execution_fingerprint

    guard = sync_guard(runtime)
    call = guard.call_fingerprint(name, args, execution_fingerprint(runtime))
    raise_decision(runtime, guard.before_call(call))
    return call


def prepare_request(runtime: Any, messages: list[Any], writer: Writer, node: str) -> None:
    from langchain_core.messages import SystemMessage

    from mokioclaw.core.run_control import check_run
    from mokioclaw.core.run_inbox import check_redirect

    check_run(runtime)
    check_redirect(runtime)
    guard = sync_guard(runtime)
    raise_decision(runtime, guard.model_round())
    marker = "MOKIO LOOP GUARD\n"
    messages[:] = [
        m
        for m in messages
        if not (isinstance(m, SystemMessage) and str(m.content).startswith(marker))
    ]
    if guard.warning:
        messages.insert(1, SystemMessage(content=marker + _MESSAGES[guard.warning]))
        if writer:
            writer(event(runtime, "warning", guard.warning, node=node))
