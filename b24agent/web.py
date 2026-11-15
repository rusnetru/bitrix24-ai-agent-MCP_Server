"""HTTP-приёмник событий Битрикс24 (aiohttp)."""

from __future__ import annotations

import hmac
import json
import logging
import re

from aiohttp import web

from b24agent.bot import BotService

log = logging.getLogger(__name__)

_KEY_PART = re.compile(r'\[([^\]]*)\]')


def unflatten(form: dict[str, str]) -> dict:
    """``data[PARAMS][MESSAGE]=...`` → ``{'data': {'PARAMS': {'MESSAGE': ...}}}``"""
    result: dict = {}
    for flat, value in form.items():
        head = flat.split('[', 1)[0]
        parts = [head, *_KEY_PART.findall(flat[len(head):])]
        node = result
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = node[part] = {}
            node = nxt
        node[parts[-1]] = value
    return result


def create_app(bot: BotService) -> web.Application:
    secret = bot.s.event_secret

    async def events(request: web.Request) -> web.Response:
        if secret and not hmac.compare_digest(request.query.get('secret', ''), secret):
            log.warning('Событие с неверным secret от %s', request.remote)
            return web.Response(status=403, text='forbidden')
        if request.content_type == 'application/json':
            event = await request.json()
        else:
            event = unflatten({k: str(v) for k, v in (await request.post()).items()})
        if log.isEnabledFor(logging.DEBUG):
            log.debug('raw event: %s', json.dumps(event, ensure_ascii=False)[:3000])
        bot.submit(event)
        return web.Response(text='OK')

    async def health(_: web.Request) -> web.Response:
        return web.json_response({'ok': True, 'bot_id': bot.imbot.bot_id})

    app = web.Application(client_max_size=10 * 1024 * 1024)
    app.router.add_post('/bitrix/events', events)
    app.router.add_get('/health', health)
    return app
