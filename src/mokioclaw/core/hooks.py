"""Explicit, bounded hooks. Only pre hooks may block a tool."""

from __future__ import annotations

import fnmatch
import importlib
import inspect
import io
import multiprocessing
import re
import time
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, dataclass, field
from typing import Any

EVENTS = {"PreToolUse", "PostToolUse", "RunFinished"}
_CALLBACKS: dict[str, list[tuple[str, str, str]]] = {}


@dataclass(frozen=True)
class HookContext:
    event: str
    tool_name: str = ""
    tool_call_id: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    result: Any = None
    status: str = ""


@dataclass(frozen=True)
class HookDecision:
    action: str = "warn"
    message: str = ""


def register_hook(event: str, callback: Callable, *, name: str | None = None):
    if (
        event not in EVENTS
        or not inspect.isfunction(callback)
        or callback.__qualname__ != callback.__name__
        or getattr(importlib.import_module(callback.__module__), callback.__name__, None)
        is not callback
    ):
        raise ValueError("hooks require a supported event and importable module-level callback")
    _CALLBACKS.setdefault(event, []).append(
        (name or callback.__name__, callback.__module__, callback.__qualname__)
    )


class _Tail(io.TextIOBase):
    def __init__(self):
        self.value = ""

    def write(self, value):
        self.value = (self.value + value)[-4096:]
        return len(value)


def _worker(connection, module, qualname, context):
    output = _Tail()
    try:
        with redirect_stdout(output), redirect_stderr(output):
            callback = importlib.import_module(module)
            for name in qualname.split("."):
                callback = getattr(callback, name)
            result = callback(context)
        if isinstance(result, HookDecision):
            result = asdict(result)
        if not isinstance(result, dict) or set(result) - {"action", "message"}:
            raise ValueError("hook must return HookDecision or action/message mapping")
        connection.send(
            {
                "action": result.get("action", "warn"),
                "message": str(result.get("message", ""))[:4000],
                "logs": output.value,
            }
        )
    except BaseException as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
        connection.send({"error": f"{type(exc).__name__}: {exc}"[:500], "logs": output.value})
    finally:
        connection.close()


def _call(runtime, spec, context):
    _, module, name = spec
    process_context = multiprocessing.get_context("spawn")
    reader, sender = process_context.Pipe(duplex=False)
    process = process_context.Process(
        target=_worker, args=(sender, module, name, context), daemon=True
    )
    process.start()
    sender.close()
    deadline = time.monotonic() + float(runtime.settings.get("hooks", {}).get("timeout_seconds", 2))
    try:
        while not reader.poll(0.02):
            if time.monotonic() >= deadline:
                raise TimeoutError("hook deadline exceeded")
            if runtime.run_control and runtime.run_control.cancel_event.is_set():
                raise RuntimeError("hook cancelled")
            if not process.is_alive():
                raise RuntimeError("hook worker exited without a result")
        result = reader.recv()
        if "error" in result:
            raise RuntimeError(result["error"])
        return result
    finally:
        reader.close()
        process.join(timeout=0.1)
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
        if process.is_alive():
            process.kill()
            process.join()
        process.close()


def run_hooks(runtime, context: HookContext, writer=None):
    decisions = []
    try:
        for index, rule in enumerate(runtime.settings.get("hooks", {}).get("rules", [])):
            if rule.get("event") != context.event or not fnmatch.fnmatchcase(
                context.tool_name, rule.get("tool", "*")
            ):
                continue
            if not all(
                re.search(pattern, str(context.args.get(key, "")))
                for key, pattern in rule.get("args", {}).items()
            ):
                continue
            decisions.append(
                (
                    rule.get("name", f"rule-{index}"),
                    {
                        "action": rule.get("action", "warn"),
                        "message": str(rule.get("message", ""))[:4000],
                    },
                )
            )
        for callback in _CALLBACKS.get(context.event, []):
            decisions.append((callback[0], _call(runtime, callback, context)))
    except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
        decisions.append(
            (
                "hook_error",
                {
                    "action": "block" if context.event == "PreToolUse" else "warn",
                    "message": f"{type(exc).__name__}: {exc}"[:500],
                    "failed": True,
                },
            )
        )
    blocked = None
    for name, decision in decisions:
        action = decision.get("action")
        valid = (
            {"warn", "block"}
            if context.event == "PreToolUse"
            else {"warn", "add_context"}
            if context.event == "PostToolUse"
            else {"warn", "report"}
        )
        if action not in valid:
            action = "block" if context.event == "PreToolUse" else "warn"
            decision = {
                "action": action,
                "message": "Unsupported hook action",
                "failed": True,
            }
        if action == "block":
            blocked = {
                "ok": False,
                "error_type": "tool_error" if decision.get("failed") else "permission_denied",
                "error": decision["message"],
                "retryable": False,
            }
        if action == "add_context":
            runtime.hook_context.append(decision["message"])
            runtime.hook_context[:] = runtime.hook_context[-8:]
        if writer:
            writer(
                {
                    "type": "hook_result",
                    "hook": name,
                    "event": context.event,
                    "tool_name": context.tool_name,
                    "tool_call_id": context.tool_call_id,
                    **decision,
                }
            )
    return blocked
