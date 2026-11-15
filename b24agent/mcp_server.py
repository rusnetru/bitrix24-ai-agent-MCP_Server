"""MCP-сервер (stdio) с теми же инструментами, что и у встроенного агента.

Подходит для подключения Claude Desktop / Cursor / любого MCP-клиента к коробке Битрикс24.
"""

from __future__ import annotations

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from b24agent.app import App
from b24agent.tools import ToolContext


async def run_mcp(app: App) -> None:
    server: Server = Server('bitrix24-box')
    tools = app.toolbox.available(app.settings, with_bot=False)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters) for t in tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        ctx = ToolContext(b24=app.b24, entities=app.entities, settings=app.settings, store=app.store)
        result = await app.toolbox.execute(ctx, name, arguments or {})
        return [types.TextContent(type='text', text=result)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
