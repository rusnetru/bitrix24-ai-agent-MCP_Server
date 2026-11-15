"""Командная строка: check | setup | serve | ask | mcp | unregister."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from aiohttp import web

from b24agent.agent import Agent
from b24agent.app import App
from b24agent.bitrix import BitrixError
from b24agent.bot import BotService
from b24agent.config import Settings, load_settings
from b24agent.imbot import ImBot
from b24agent.llm import LLM
from b24agent.tools import ToolContext
from b24agent.web import create_app

log = logging.getLogger('b24agent')

TIMELINE_EVENTS = ['ONCRMTIMELINECOMMENTADD']


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
        need = {'crm', 'task', 'user', 'im', 'imbot'}
        missing = need - set(scope or [])
        print(f'  права вебхука: {", ".join(sorted(scope or []))}')
        if missing:
            ok = False
            print(f'  ✘ не хватает прав: {", ".join(sorted(missing))}')
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
    print(f'Бот: {"ID " + str(bot_id) if bot_id else "не зарегистрирован (python -m b24agent setup)"}')
    print(f'Адрес для событий: {s.events_url if s.public_url else "— задайте PUBLIC_URL"}')
    return 0 if ok else 1


async def cmd_setup(app: App) -> int:
    s = app.settings
    imbot = ImBot(app.b24, s, app.store)
    bot_id = await imbot.register()
    print(f'✔ Бот «{s.bot_name}» зарегистрирован, ID {bot_id}. Обработчик: {s.events_url}')
    print('  Найдите его в мессенджере: Чаты → поиск по имени бота, или добавьте в групповой чат.')
    bound = []
    for event in TIMELINE_EVENTS:
        try:
            await app.b24.call('event.bind', {'event': event, 'handler': s.events_url})
            bound.append(event)
        except BitrixError as exc:
            if 'already' in exc.description.lower() or 'HANDLER_ALREADY' in exc.code:
                bound.append(event)
            else:
                print(f'  ! event.bind {event}: {exc.code} {exc.description}')
    if bound:
        print(f'✔ Подписка на события таймлайна: {", ".join(bound)}')
    else:
        print('  Подписку на комментарии таймлайна через вебхук оформить не удалось. Варианты:\n'
              '  1) Приложения → Разработчикам → Другое → Исходящий вебхук: событие\n'
              f'     ONCRMTIMELINECOMMENTADD, URL обработчика: {s.events_url}\n'
              '  2) или включите опрос: TIMELINE_POLL_INTERVAL=30 (работает для отслеживаемых объектов).')
    return 0


async def cmd_unregister(app: App) -> int:
    imbot = ImBot(app.b24, app.settings, app.store)
    await imbot.load()
    await imbot.unregister()
    for event in TIMELINE_EVENTS:
        try:
            await app.b24.call('event.unbind', {'event': event, 'handler': app.settings.events_url})
        except BitrixError:
            pass
    print('✔ Бот удалён')
    return 0


def _bot_service(app: App) -> BotService:
    imbot = ImBot(app.b24, app.settings, app.store)
    agent = Agent(LLM(app.settings), app.toolbox)
    return BotService(app.b24, app.settings, app.store, app.entities, app.toolbox, agent, imbot)


async def cmd_serve(app: App) -> int:
    s = app.settings
    bot = _bot_service(app)
    if not await bot.imbot.load():
        log.warning('Бот не зарегистрирован — выполните: python -m b24agent setup')
    runner = web.AppRunner(create_app(bot))
    await runner.setup()
    await web.TCPSite(runner, s.http_host, s.http_port).start()
    log.info('Приём событий: http://%s:%s/bitrix/events (снаружи: %s)', s.http_host, s.http_port,
             s.events_url if s.public_url else 'PUBLIC_URL не задан')
    poller = asyncio.create_task(bot.poll_timelines()) if s.timeline_poll_interval > 0 else None
    try:
        await asyncio.Event().wait()
    finally:
        if poller:
            poller.cancel()
        await runner.cleanup()
        await bot.agent.llm.close()
    return 0


async def cmd_ask(app: App, text: str, user_id: int | None) -> int:
    """Проверка агента из консоли, без мессенджера."""
    bot = _bot_service(app)
    await bot.imbot.load()
    ctx = ToolContext(b24=app.b24, entities=app.entities, settings=app.settings, store=app.store, user_id=user_id)
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
