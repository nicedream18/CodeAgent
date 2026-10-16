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
        raise ValueError("MOKIO_MCP_SERVERS must be a JSON list")  # noqa: TRY004 — preserve the public ValueError validation contract
    servers = []
    for item in value:
        if not isinstance(item, dict) or not item.get("name") or not item.get("command"):
            raise ValueError("each MCP server needs name and command fields")
        servers.append(
            {
                "name": str(item["name"]),
                "command": str(item["command"]),
                "args": [str(arg) for arg in item.get("args", [])],
                "env": {str(k): str(v) for k, v in item.get("env", {}).items()},
                "cwd": str(item.get("cwd", "")),
                "description": str(item.get("description", "")),
            }
        )
    if len({_safe_name(s["name"]) for s in servers}) != len(servers):
        raise ValueError("MCP server names must have distinct normalized identifiers")
    return servers


def refresh_catalog_config(runtime):
    fingerprint = hashlib.sha256(
        json.dumps(runtime.mcp_servers, sort_keys=True).encode()
    ).hexdigest()
    if runtime.mcp_config_digest != fingerprint:
        runtime.mcp_config_digest = fingerprint
        runtime.mcp_loaded_tools.clear()
        for key in list(runtime.tool_cache):
            if key.startswith("mcp:") or key in {"write", "read_only"}:
                del runtime.tool_cache[key]
        runtime.mcp_catalog_version += 1


def build_mcp_tools(runtime: RuntimeState, servers=None) -> list[StructuredTool]:
    refresh_catalog_config(runtime)
    servers = runtime.mcp_servers if servers is None else servers
    key = "mcp:" + hashlib.sha256(json.dumps(servers, sort_keys=True).encode()).hexdigest()
    if key in runtime.tool_cache:
        return runtime.tool_cache[key]
    result: list[StructuredTool] = []
    for server in servers:
        try:
            check_run(runtime)
            tools = _operation(
                runtime, server, "discover", lambda client: client.list_tools()
            ).tools
        except RunInterrupted:
            raise
        except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
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
                    digest = hashlib.sha256(f"{server['name']}:{remote.name}".encode()).hexdigest()[
                        :8
                    ]
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
            except Exception as exc:  # noqa: BLE001 — fault boundary; control signals are handled separately
                runtime.mcp_discovery_errors.append(
                    {"server": server["name"], "error": f"{type(exc).__name__}: {exc}"}
                )
    runtime.tool_cache[key] = result
    return result


def search_mcp_tools(runtime, query="", server=None, limit=5, refresh=False):
    refresh_catalog_config(runtime)
    if not 1 <= int(limit) <= 20:
        return {"ok": False, "error": "limit must be between 1 and 20"}
    available = runtime.mcp_servers
    selected = (
        [s for s in available if s["name"] == server]
        if server
        else [
            s
            for s in available
            if query
            and any(
                word in (s["name"] + " " + s.get("description", "")).lower()
                for word in query.lower().split()
            )
        ]
    )
    if not selected:
        return {
            "ok": True,
            "tools": [],
            "servers": [
                {"name": s["name"], "description": s.get("description", "")} for s in available
            ],
            "message": "Specify a server to discover its tools.",
        }
    if refresh:
        for key in list(runtime.tool_cache):
            if key.startswith("mcp:"):
                del runtime.tool_cache[key]
        runtime.mcp_loaded_tools.clear()
    found = build_mcp_tools(runtime, selected)
    words = query.lower().split()
    ranked = sorted(
        found,
        key=lambda t: (
            -sum(w in (t.name + " " + t.description).lower() for w in words),
            t.name,
        ),
    )
    loaded = []
    changed = bool(refresh)
    for tool in ranked:
        if (
            runtime.active_agent_tools is not None
            and tool.name not in runtime.active_agent_tools
            and "MCP_*" not in runtime.active_agent_tools
        ):
            continue
        if runtime.active_skill_tools is not None and tool.name not in runtime.active_skill_tools:
            continue
        if (
            runtime.active_skill_permissions is not None
            and "mcp" not in runtime.active_skill_permissions
        ):
            continue
        if runtime.tool_policies.get(tool.name) == "deny":
            continue
        changed = changed or runtime.mcp_loaded_tools.get(tool.name) is not tool
        runtime.mcp_loaded_tools[tool.name] = tool
        loaded.append({"name": tool.name, "description": tool.description})
        if len(loaded) >= int(limit):
            break
    from mokioclaw.core.tool_context import current_tool_call_context

    if changed:
        runtime.mcp_catalog_version += 1
        runtime.context_dirty = True
    context = current_tool_call_context()
    if context and context.event_writer:
        context.event_writer({"type": "tool_catalog_loaded", "tools": [t["name"] for t in loaded]})
        emit_mcp_diagnostics(runtime, context.event_writer)
    return {
        "ok": True,
        "tools": loaded,
        "message": "Selected schemas become available in the next model request.",
    }


def build_tool_search(runtime):
    return StructuredTool.from_function(
        name="ToolSearchTool",
        func=lambda query="", server=None, limit=5, refresh=False: search_mcp_tools(
            runtime, query, server, limit, refresh
        ),
        description="Discover MCP tools on demand. Supply server name or search its purpose; schemas are loaded for your next request. Servers: "
        + json.dumps(
            [
                {"name": s["name"], "description": s.get("description", "")}
                for s in runtime.mcp_servers
            ],
            ensure_ascii=False,
        ),
    )


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
    if any(item in text for item in ("closed", "endofstream", "brokenpipe", "connection")):
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
            async with Client(stdio_client(_server_params(server), errlog=errlog)) as client:
                return await operation(client)

    try:
        response = control.run_async(perform)
        diagnostic["status"] = "failed" if getattr(response, "is_error", False) else "finished"
        diagnostic["reason"] = "server_error" if getattr(response, "is_error", False) else ""
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
        annotation = _python_type(definition.get("type")) if isinstance(definition, dict) else Any
        default = ... if key in required else None
        description = definition.get("description", "") if isinstance(definition, dict) else ""
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
