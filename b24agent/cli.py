"""Командная строка: check | setup | serve | ask | mcp | tools | unregister."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from aiohttp import web

from b24agent.agent import Agent
from b24agent.app import App
from b24agent.bitrix import BitrixError
from b24agent.bot import BotService
from b24agent.config import Settings, load_settings
from b24agent.imbot import make_imbot
from b24agent.llm import LLM
from b24agent.tools import ToolContext
from b24agent.web import create_app

log = logging.getLogger('b24agent')

def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        stream=sys.stderr,
    )
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)


async def cmd_check(app: App) -> int:
    s = app.settings
    ok = True
    print(f'Портал: {s.portal_url}')
    try:
        prof = await app.b24.call('profile')
        print(f'  ✔ вебхук работает, пользователь: {prof.get("NAME")} {prof.get("LAST_NAME")} (ID {prof.get("ID")})')
        scope = await app.b24.call('scope')
        need = {'crm', 'task', 'user', 'imbot'}
        nice = {'im', 'department', 'sonet_group', 'calendar', 'bizproc', 'lists'}
        have = set(scope or [])
        print(f'  права вебхука: {", ".join(sorted(have))}')
        if need - have:
            ok = False
            print(f'  ✘ не хватает обязательных прав: {", ".join(sorted(need - have))}')
        if nice - have:
            print(f'  ! без прав {", ".join(sorted(nice - have))} часть инструментов вернёт insufficient_scope')
        if s.bot_api == 'v2':
            info = await app.b24.method_info('imbot.v2.Event.get')
            if info.get('isExisting'):
                rev = await app.b24.call('imbot.v2.Revision.get')
                print(f'  ✔ imbot.v2 есть (revision {rev})')
            else:
                ok = False
                print('  ✘ на портале нет imbot.v2 — обновите модуль im или поставьте BOT_API=v1 (нужен PUBLIC_URL)')
        v3 = await app.b24.method_info('rest.documentation.openapi')
        print('  ✔ REST 3.0 доступен' if v3.get('isExisting') else '  ! REST 3.0 не найден (используется REST v2)')
        types = [e for e in await app.entities.all() if e.is_dynamic]
        print(f'  ✔ смарт-процессов: {len(types)}' + (': ' + ', '.join(f'{e.title} ({e.id})' for e in types)
                                                    if types else ''))
    except BitrixError as exc:
        ok = False
        print(f'  ✘ ошибка Битрикс24: {exc}')
    except Exception as exc:
        ok = False
        print(f'  ✘ нет связи с порталом: {exc!r}. Для самоподписанного сертификата: '
              'BITRIX_VERIFY_SSL=false или путь к CA')
    print(f'LLM: {s.llm_base_url or "—"} модель {s.llm_model or "—"}')
    if s.llm_base_url and s.llm_model:
        llm = LLM(s)
        try:
            msg = await llm.chat([{'role': 'user', 'content': 'Ответь одним словом: работает?'}])
            print(f'  ✔ ответ модели: {msg.get("content", "")[:80]!r}')
            test_tool = [{'type': 'function', 'function': {
                'name': 'ping', 'description': 'Проверка связи. Вызови этот инструмент.',
                'parameters': {'type': 'object', 'properties': {}, 'required': []}}}]
            try:
                msg = await llm.chat([{'role': 'user', 'content': 'Вызови инструмент ping.'}], test_tool)
                native = bool(msg.get('tool_calls')) or '<tool_call>' in (msg.get('content') or '')
                print('  ✔ нативный вызов инструментов поддерживается' if native else
                      '  ! модель не вызвала инструмент — при проблемах поставьте LLM_TOOL_MODE=json')
            except Exception as exc:
                print(f'  ! tools не поддерживаются ({exc}); будет использован LLM_TOOL_MODE=json')
        except Exception as exc:
            ok = False
            print(f'  ✘ ошибка LLM: {exc}')
        finally:
            await llm.close()
    else:
        ok = False
        print('  ✘ задайте LLM_BASE_URL и LLM_MODEL')
    bot_id = await app.store.get('bot_id')
    print(f'Бот ({s.bot_api}, {s.bot_event_mode if s.bot_api == "v2" else "push"}): '
          f'{"ID " + str(bot_id) if bot_id else "не зарегистрирован (b24agent setup)"}')
    if s.bot_api == 'v1' or s.bot_event_mode == 'webhook':
        print(f'Адрес для событий: {s.events_url if s.public_url else "— задайте PUBLIC_URL"}')
    print(f'Инструментов: {len(app.toolbox.available(s, with_bot=True))} (b24agent tools — список)')
    return 0 if ok else 1


async def cmd_setup(app: App) -> int:
    s = app.settings
    imbot = make_imbot(app.b24, s, app.store)
    bot_id = await imbot.register()
    if imbot.supports_fetch:
        mode = 'fetch (агент сам забирает события, входящий доступ к агенту не нужен)'
    else:
        mode = f'push на {s.events_url}'
    print(f'✔ Бот «{s.bot_name}» зарегистрирован ({s.bot_api}), ID {bot_id}. События: {mode}')
    print('  Найдите его в мессенджере: Чаты → поиск по имени бота, или добавьте в групповой чат.')
    print('Таймлайн CRM: подписаться на события через входящий вебхук нельзя (event.bind → WRONG_AUTH_TYPE).')
    print('  Варианты получения комментариев:')
    print('  • TIMELINE_SCAN_TYPES=deal,1038 — сканировать недавно активные элементы (закрытый контур);')
    print('  • TIMELINE_POLL_INTERVAL=30 — опрос объектов, включённых командой «следи за таймлайном ...»;')
    if s.public_url:
        print('  • исходящий вебхук (Разработчикам → Другое → Исходящий вебхук) на ONCRMTIMELINECOMMENTADD,')
        print(f'    URL {s.events_url} — только если портал может достучаться до агента.')
    return 0


async def cmd_unregister(app: App) -> int:
    imbot = make_imbot(app.b24, app.settings, app.store)
    await imbot.load()
    await imbot.unregister()
    print('✔ Бот удалён')
    return 0


def _bot_service(app: App) -> BotService:
    imbot = make_imbot(app.b24, app.settings, app.store)
    agent = Agent(LLM(app.settings), app.toolbox)
    return BotService(app.b24, app.settings, app.store, app.entities, app.toolbox, agent, imbot)


async def cmd_serve(app: App) -> int:
    s = app.settings
    bot = _bot_service(app)
    if not await bot.imbot.load():
        log.warning('Бот не зарегистрирован — выполните: b24agent setup')
    runner = web.AppRunner(create_app(bot))
    await runner.setup()
    await web.TCPSite(runner, s.http_host, s.http_port).start()
    log.info('HTTP: http://%s:%s (/health, /bitrix/events)', s.http_host, s.http_port)
    tasks = []
    if bot.imbot.supports_fetch and bot.imbot.bot_id:
        tasks.append(asyncio.create_task(bot.fetch_loop()))
    if s.timeline_poll_interval > 0:
        tasks.append(asyncio.create_task(bot.poll_watched()))
    if s.timeline_scan_types:
        tasks.append(asyncio.create_task(bot.scan_timelines()))
    try:
        await asyncio.Event().wait()
    finally:
        for t in tasks:
            t.cancel()
        await runner.cleanup()
        await bot.agent.llm.close()
    return 0


async def cmd_tools(app: App, as_json: bool) -> int:
    tools = sorted(app.toolbox.tools.values(), key=lambda t: (t.group, t.name))
    if as_json:
        print(json.dumps([{'name': t.name, 'group': t.group, 'write': t.write, 'bot_only': t.bot_only,
                           'description': t.description, 'parameters': t.parameters} for t in tools],
                         ensure_ascii=False, indent=2))
        return 0
    active = {t.name for t in app.toolbox.available(app.settings, with_bot=True)}
    group = None
    for t in tools:
        if t.group != group:
            group = t.group
            print(f'\n[{group}]')
        flags = ''.join([' W' if t.write else '', ' bot' if t.bot_only else '',
                         '' if t.name in active else ' (выкл.)'])
        print(f'  {t.name:<24}{flags}  {t.description.split(". ")[0][:90]}')
    return 0


async def cmd_ask(app: App, text: str, user_id: int | None) -> int:
    """Проверка агента из консоли, без мессенджера."""
    bot = _bot_service(app)
    await bot.imbot.load()
    ctx = ToolContext(b24=app.b24, entities=app.entities, settings=app.settings, store=app.store,
                      tb=app.toolbox, user_id=user_id)
    try:
        print(await bot.agent.run(ctx, [], text, ['Канал: консоль администратора (тест).']))
    finally:
        await bot.agent.llm.close()
    return 0


async def _main(args: argparse.Namespace, settings: Settings) -> int:
    app = App(settings)
    try:
        if args.cmd == 'check':
            return await cmd_check(app)
        if args.cmd == 'setup':
            return await cmd_setup(app)
        if args.cmd == 'unregister':
            return await cmd_unregister(app)
        if args.cmd == 'serve':
            return await cmd_serve(app)
        if args.cmd == 'ask':
            return await cmd_ask(app, ' '.join(args.text), args.user)
        if args.cmd == 'tools':
            return await cmd_tools(app, args.json)
        if args.cmd == 'mcp':
            from b24agent.mcp_server import run_mcp
            await run_mcp(app)
            return 0
    finally:
        await app.close()
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(prog='b24agent', description='ИИ-агент и MCP-сервер для коробки Битрикс24')
    parser.add_argument('--env', help='путь к .env (по умолчанию ./.env)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check', help='проверить доступ к порталу и LLM')
    sub.add_parser('setup', help='зарегистрировать/обновить чат-бота и подписаться на события')
    sub.add_parser('serve', help='запустить приём событий (чат-бот + таймлайн)')
    sub.add_parser('mcp', help='MCP-сервер по stdio')
    sub.add_parser('unregister', help='удалить чат-бота из портала')
    tools = sub.add_parser('tools', help='список инструментов (встроенные + плагины)')
    tools.add_argument('--json', action='store_true', help='полное описание со схемами параметров')
    ask = sub.add_parser('ask', help='задать вопрос агенту из консоли')
    ask.add_argument('text', nargs='+')
    ask.add_argument('--user', type=int, help='ID пользователя, от имени которого спрашиваем')
    args = parser.parse_args()

    settings = load_settings(args.env)
    _setup_logging(settings.log_level)
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.exit(asyncio.run(_main(args, settings)))
    except KeyboardInterrupt:
        pass
