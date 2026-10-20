from __future__ import annotations

import anyio
from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server


async def list_tools(context, params) -> types.ListToolsResult:
    del context, params
    return types.ListToolsResult(
        tools=[
            types.Tool(
                name="echo",
                description="Return the supplied text with a short prefix.",
                inputSchema={
                    "type": "object",
                    "properties": {"text": {"type": "string", "description": "Text to echo"}},
                    "required": ["text"],
                },
            )
        ]
    )


async def call_tool(context, params) -> types.CallToolResult:
    del context
    if params.name != "echo":
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"unknown tool: {params.name}")],
            isError=True,
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=f"echo: {params.arguments['text']}")]
    )


server = Server("mokioclaw-example")


@server.list_tools()
async def sdk_list_tools():
    return (await list_tools(None, None)).tools


@server.call_tool()
async def sdk_call_tool(name: str, arguments: dict):
    return await call_tool(None, types.CallToolRequestParams(name=name, arguments=arguments))


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    anyio.run(main)
