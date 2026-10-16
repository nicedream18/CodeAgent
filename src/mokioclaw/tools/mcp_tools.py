from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import os
import re
import threading
import time
from typing import Any
from uuid import uuid4

from langchain_core.tools import StructuredTool
from mcp import Client
from mcp.client.stdio import StdioServerParameters, stdio_client
from pydantic import Field, create_model

from mokioclaw.core.artifacts import DiagnosticArtifact
from mokioclaw.core.run_control import (
    RunControl,
    RunInterrupted,
    check_run,
    run_control,
)
from mokioclaw.core.state import RuntimeState
from mokioclaw.core.tool_context import current_tool_call_context


def parse_mcp_servers(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("MOKIO_MCP_SERVERS must be a JSON list") from exc
    if not isinstance(value, list):
        raise ValueError("MOKIO_MCP_SERVERS must be a JSON list")
    servers = []
    for item in value:
        if (
            not isinstance(item, dict)
            or not item.get("name")
            or not item.get("command")
        ):
            raise ValueError("each MCP server needs name and command fields")
        servers.append(
            {
                "name": str(item["name"]),
                "command": str(item["command"]),
                "args": [str(arg) for arg in item.get("args", [])],
                "env": {str(k): str(v) for k, v in item.get("env", {}).items()},
                "cwd": str(item.get("cwd", "")),
            }
        )
    return servers


def build_mcp_tools(runtime: RuntimeState) -> list[StructuredTool]:
    if "mcp" in runtime.tool_cache:
        return runtime.tool_cache["mcp"]
    result: list[StructuredTool] = []
    for server in runtime.mcp_servers:
        try:
            check_run(runtime)
            tools = _operation(
                runtime, server, "discover", lambda client: client.list_tools()
            ).tools
        except RunInterrupted:
            raise
        except Exception as exc:
            runtime.mcp_discovery_errors.append(
                {"server": server["name"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        for remote in tools:
            try:
                safe_server = _safe_name(server["name"])
                safe_tool = _safe_name(remote.name)
                public_name = f"MCP_{safe_server}_{safe_tool}"
                if len(public_name) > 64:
                    digest = hashlib.sha256(
                        f"{server['name']}:{remote.name}".encode()
                    ).hexdigest()[:8]
                    public_name = public_name[:55] + "_" + digest
                if any(tool.name == public_name for tool in result):
                    raise ValueError(f"duplicate MCP tool name: {public_name}")
                input_schema = getattr(remote, "input_schema", None) or getattr(
                    remote, "inputSchema", {}
                )
                schema = _schema_model(f"{safe_server}_{safe_tool}_Input", input_schema)
                result.append(
                    _make_mcp_tool(
                        public_name,
                        remote.name,
                        server,
                        schema,
                        remote.description,
                        runtime,
                    )
                )
            except Exception as exc:
                runtime.mcp_discovery_errors.append(
                    {"server": server["name"], "error": f"{type(exc).__name__}: {exc}"}
                )
    runtime.tool_cache["mcp"] = result
    return result


def _make_mcp_tool(public_name, remote_name, server, schema, description, runtime):
    def invoke(**kwargs):
        response = _operation(
            runtime,
            server,
            "call",
            lambda client: client.call_tool(remote_name, kwargs),
        )
        content = []
        for block in response.content:
            text = getattr(block, "text", None)
            content.append(text if text is not None else block.model_dump(mode="json"))
        return {
            "ok": not response.is_error,
            "content": content,
            "structured_content": response.structured_content,
            "error_type": "tool_error" if response.is_error else "",
            "error": str(content)[:1000] if response.is_error else "",
            "retryable": False,
        }

    return StructuredTool.from_function(
        name=public_name,
        func=invoke,
        args_schema=schema,
        description=f"MCP server {server['name']}: {description or remote_name}",
    )


def emit_mcp_diagnostics(runtime, writer):
    pending, runtime.mcp_diagnostics = runtime.mcp_diagnostics, []
    for event in pending:
        writer(event)


def _reason(exc):
    if isinstance(exc, BaseExceptionGroup):
        return _reason(exc.exceptions[0])
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, FileNotFoundError):
        return "launch_failed"
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(
        item in text for item in ("closed", "endofstream", "brokenpipe", "connection")
    ):
        return "connection_closed"
    return "protocol_error"


def _operation(runtime, server, phase, operation):
    check_run(runtime)
    capture = DiagnosticArtifact(runtime.workspace, "mcp-stderr")
    read_fd, write_fd = os.pipe()
    errlog = os.fdopen(write_fd, "w", encoding="utf-8")

    drained = threading.Event()
    os.set_blocking(read_fd, False)

    def drain():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with os.fdopen(read_fd, "rb", buffering=0) as source:
            deadline = None
            while True:
                if drained.is_set():
                    deadline = deadline or time.monotonic() + 0.5
                    if time.monotonic() >= deadline:
                        break
                try:
                    data = os.read(source.fileno(), 65536)
                except BlockingIOError:
                    if drained.wait(0.01):
                        break
                    continue
                if not data:
                    break
                capture.write(decoder.decode(data).encode("utf-8"))
            capture.write(decoder.decode(b"", final=True).encode("utf-8"))

    reader = threading.Thread(target=drain, name="mokio-mcp-stderr", daemon=True)
    reader.start()
    control = run_control(runtime)
    owned = control is None
    control = control or RunControl()
    context = current_tool_call_context()
    diagnostic = {
        "type": "mcp_diagnostic",
        "operation_id": uuid4().hex,
        "server": server["name"],
        "phase": phase,
        "tool_call_id": context.tool_call_id if context else "",
        "status": "failed",
        "reason": "",
        "error": "",
    }
    started = time.monotonic()

    async def perform():
        async with asyncio.timeout(runtime.mcp_timeout_seconds):
            async with Client(
                stdio_client(_server_params(server), errlog=errlog)
            ) as client:
                return await operation(client)

    try:
        response = control.run_async(perform)
        diagnostic["status"] = (
            "failed" if getattr(response, "is_error", False) else "finished"
        )
        diagnostic["reason"] = (
            "server_error" if getattr(response, "is_error", False) else ""
        )
        return response
    except (RunInterrupted, KeyboardInterrupt):
        diagnostic.update(status="interrupted", reason="cancelled")
        raise
    except Exception as exc:
        reason = _reason(exc)
        diagnostic.update(reason=reason, error=f"{type(exc).__name__}: {exc}"[:500])
        if reason == "timeout":
            raise TimeoutError(f"MCP {server['name']} {phase} timed out") from exc
        raise RuntimeError(f"MCP {server['name']} {phase}: {reason}: {exc}") from exc
    finally:
        if owned:
            control.close()
        errlog.close()
        drained.set()
        reader.join(timeout=1)
        diagnostic.update(capture.finish())
        diagnostic.update(
            stderr=capture.tail.decode("utf-8", errors="ignore"),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        if control.event_sink is not None:
            control.event_sink(diagnostic)
        if context is not None and context.event_writer is not None:
            context.event_writer(diagnostic)
        else:
            runtime.mcp_diagnostics.append(diagnostic)


def _server_params(server: dict[str, Any]) -> StdioServerParameters:
    return StdioServerParameters(
        command=server["command"],
        args=list(server.get("args", [])),
        env=server.get("env") or None,
        cwd=server.get("cwd") or None,
    )


def _schema_model(name: str, schema: dict[str, Any]):
    properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
    required = set(schema.get("required", [])) if isinstance(schema, dict) else set()
    fields = {}
    for key, definition in properties.items():
        annotation = (
            _python_type(definition.get("type"))
            if isinstance(definition, dict)
            else Any
        )
        default = ... if key in required else None
        description = (
            definition.get("description", "") if isinstance(definition, dict) else ""
        )
        fields[key] = (annotation, Field(default=default, description=description))
    return create_model(name, **fields)


def _python_type(type_name: str | None):
    return {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "object": dict[str, Any],
        "array": list[Any],
    }.get(type_name, Any)


def _safe_name(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]+", "_", value).strip("_")[:48] or "tool"
