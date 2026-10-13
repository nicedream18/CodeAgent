from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from mcp import Client
from mcp.client.stdio import StdioServerParameters

from mokioclaw.core.state import RuntimeState


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
            tools = asyncio.run(_list_tools(server, runtime.mcp_timeout_seconds))
        except Exception as exc:
            runtime.mcp_discovery_errors.append(
                {"server": server["name"], "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        for remote in tools:
            safe_server = _safe_name(server["name"])
            safe_tool = _safe_name(remote.name)
            public_name = f"MCP_{safe_server}_{safe_tool}"
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
                    runtime.mcp_timeout_seconds,
                )
            )
    runtime.tool_cache["mcp"] = result
    return result


def _make_mcp_tool(public_name, remote_name, server, schema, description, timeout):
    def invoke(**kwargs):
        return _invoke_tool(server, remote_name, kwargs, timeout)

    return StructuredTool.from_function(
        name=public_name,
        func=invoke,
        args_schema=schema,
        description=f"MCP server {server['name']}: {description or remote_name}",
    )


async def _list_tools(server: dict[str, Any], timeout: float):
    async with asyncio.timeout(timeout):
        async with Client(_server_params(server)) as client:
            return (await client.list_tools()).tools


async def _call_tool(
    server: dict[str, Any], name: str, arguments: dict[str, Any], timeout: float
):
    async with asyncio.timeout(timeout):
        async with Client(_server_params(server)) as client:
            return await client.call_tool(name, arguments)


def _invoke_tool(
    server: dict[str, Any], name: str, arguments: dict[str, Any], timeout: float
) -> dict[str, Any]:
    response = asyncio.run(_call_tool(server, name, arguments, timeout))
    content = []
    for block in response.content:
        text = getattr(block, "text", None)
        content.append(text if text is not None else block.model_dump(mode="json"))
    return {
        "ok": not response.is_error,
        "content": content,
        "structured_content": response.structured_content,
        "error_type": "mcp_tool_error" if response.is_error else "",
        "retryable": False,
    }


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
