"""MCP-сервер (stdio) с теми же инструментами, что и у встроенного агента.

Подходит для подключения Claude Desktop / Cursor / любого MCP-клиента к коробке Битрикс24.
"""

from __future__ import annotations

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from b24agent.app import App
from b24agent.tools import ToolContext


async def run_mcp(app: App, as_user: int | None = None) -> None:
    """as_user — работать как личный ассистент сотрудника (его вебхук, его права, его память)."""
    profile = app.profiles.service
    if as_user:
        profile = app.profiles.for_user(as_user)
        if profile is None:
            msg = f'Для пользователя {as_user} нет рабочего личного ассистента (см. b24agent personal)'
            raise SystemExit(msg)
    server: Server = Server('bitrix24-box' if not profile.personal else f'bitrix24-personal-{as_user}')
    tools = app.toolbox.available(profile.settings, with_bot=False, personal=profile.personal and profile.memory)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=t.name, description=t.description, inputSchema=t.parameters) for t in tools]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
        ctx = ToolContext(b24=profile.b24, entities=app.entities, settings=profile.settings, store=app.store,
                          tb=app.toolbox, user_id=profile.user_id, profile=profile)
        result = await app.toolbox.execute(ctx, name, arguments or {})
        return [types.TextContent(type='text', text=result)]

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
