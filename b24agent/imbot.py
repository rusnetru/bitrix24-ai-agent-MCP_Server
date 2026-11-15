"""Чат-бот Битрикс24, зарегистрированный через входящий вебхук.

Две реализации с одинаковым интерфейсом:

- :class:`ImBotV2` — платформа ``imbot.v2.*`` (рекомендуется). Режим ``fetch``: агент сам забирает
  события методом ``imbot.v2.Event.get`` — входящий доступ к агенту не нужен, подходит для закрытого
  контура. Режим ``webhook``: портал присылает события POST-запросом на ``PUBLIC_URL``.
  Авторизация бота при вызове через вебхук — ``botId`` + ``botToken``.
- :class:`ImBotV1` — устаревший ``imbot.*`` (развитие остановлено), только push. Через вебхук каждый
  вызов требует ``CLIENT_ID``. Оставлен для старых коробок без imbot.v2.
"""

from __future__ import annotations

import logging
import secrets
from typing import Any

from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.store import Store

log = logging.getLogger(__name__)

MAX_MESSAGE = 9000


def split_text(text: str, size: int = MAX_MESSAGE) -> list[str]:
    text = text.strip()
    out = []
    while len(text) > size:
        cut = text.rfind('\n', 0, size)
        if cut < size // 2:
            cut = size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


class ImBotBase:
    api = ''
    supports_fetch = False

    def __init__(self, b24: Bitrix, settings: Settings, store: Store):
        self.b24 = b24
        self.s = settings
        self.store = store
        self.bot_id: int | None = None

    async def load(self) -> int | None:
        self.bot_id = await self.store.get(f'bot_id_{self.api}')
        if self.bot_id is None and self.api == 'v1':  # совместимость с данными версии 2.0.0
            self.bot_id = await self.store.get('bot_id')
        return self.bot_id

    async def _save(self) -> None:
        await self.store.set(f'bot_id_{self.api}', self.bot_id)
        await self.store.set('bot_id', self.bot_id)

    # интерфейс
    async def register(self) -> int: raise NotImplementedError
    async def unregister(self) -> None: raise NotImplementedError
    async def send(self, dialog_id: str, text: str, keyboard: list | None = None) -> None: raise NotImplementedError
    async def typing(self, dialog_id: str) -> None: raise NotImplementedError
    async def create_chat(self, title: str, users: list[int], description: str = '', message: str = '',
                          entity_type: str = '', entity_id: str = '') -> str: raise NotImplementedError

    async def fetch(self, offset: int, limit: int = 100) -> dict:
        raise NotImplementedError


class ImBotV2(ImBotBase):
    api = 'v2'

    @property
    def supports_fetch(self) -> bool:  # type: ignore[override]
        return self.s.bot_event_mode == 'fetch'

    async def token(self) -> str:
        if self.s.bot_token:
            return self.s.bot_token
        tok = await self.store.get('bot_token_v2')
        if not tok:
            tok = secrets.token_urlsafe(24)
            await self.store.set('bot_token_v2', tok)
        return tok

    async def call(self, method: str, params: dict | None = None, *, with_bot: bool = True) -> Any:
        payload = {'botToken': await self.token(), **(params or {})}
        if with_bot and self.bot_id:
            payload.setdefault('botId', self.bot_id)
        return await self.b24.call(method, payload)

    def _fields(self) -> dict:
        fields: dict = {
            'properties': {'name': self.s.bot_name, 'lastName': self.s.bot_last_name,
                           'workPosition': self.s.bot_work_position},
            'eventMode': self.s.bot_event_mode,
            'isSupportOpenline': self.s.bot_openline,
        }
        if self.s.bot_event_mode == 'webhook':
            if not self.s.public_url:
                msg = 'BOT_EVENT_MODE=webhook требует PUBLIC_URL (адрес агента, доступный порталу)'
                raise RuntimeError(msg)
            fields['webhookUrl'] = self.s.events_url
        return fields

    async def find_existing(self) -> int | None:
        try:
            res = await self.call('imbot.v2.Bot.list', {'limit': 0}, with_bot=False)
        except BitrixError:
            return None
        for bot in (res or {}).get('bots', []):
            if str(bot.get('code')) == self.s.bot_code:
                return int(bot['id'])
        return None

    async def register(self) -> int:
        existing = await self.find_existing()
        if existing:
            self.bot_id = existing
            await self.call('imbot.v2.Bot.update', {'botId': existing, 'fields': self._fields()})
            log.info('Бот %s (imbot.v2) уже есть, ID=%s — параметры обновлены', self.s.bot_code, existing)
        else:
            res = await self.call('imbot.v2.Bot.register', {'fields': {
                'code': self.s.bot_code, 'botToken': await self.token(), 'type': self.s.bot_type,
                **self._fields()}}, with_bot=False)
            self.bot_id = int(res['bot']['id'])
            log.info('Бот зарегистрирован (imbot.v2), ID=%s', self.bot_id)
        await self._save()
        return self.bot_id

    async def unregister(self) -> None:
        bot_id = self.bot_id or await self.find_existing()
        if bot_id:
            await self.call('imbot.v2.Bot.unregister', {'botId': bot_id})
        self.bot_id = None
        await self._save()

    async def send(self, dialog_id: str, text: str, keyboard: list | None = None) -> None:
        chunks = split_text(text) or ['…']
        for i, chunk in enumerate(chunks):
            fields: dict = {'message': chunk}
            if keyboard and i == len(chunks) - 1:
                fields['keyboard'] = keyboard
            await self.call('imbot.v2.Chat.Message.send', {'dialogId': dialog_id, 'fields': fields})

    async def typing(self, dialog_id: str) -> None:
        try:
            await self.call('imbot.v2.Chat.InputAction.notify', {
                'dialogId': dialog_id, 'statusMessageCode': 'IMBOT_AGENT_ACTION_THINKING', 'duration': 60})
        except BitrixError as exc:
            log.debug('InputAction.notify: %s', exc)

    async def create_chat(self, title: str, users: list[int], description: str = '', message: str = '',
                          entity_type: str = '', entity_id: str = '') -> str:
        res = await self.call('imbot.v2.Chat.add', {'fields': {
            'title': title[:250], 'description': description, 'userIds': users}})
        dialog = str(res['chat']['dialogId'])
        if message:
            await self.send(dialog, message)
        return dialog

    async def fetch(self, offset: int, limit: int = 100) -> dict:
        return await self.call('imbot.v2.Event.get', {'offset': offset, 'limit': limit}) or {}


class ImBotV1(ImBotBase):
    api = 'v1'

    async def call(self, method: str, params: dict | None = None) -> Any:
        payload = {'CLIENT_ID': self.s.bot_client_id, **(params or {})}
        if self.bot_id and 'BOT_ID' not in payload:
            payload['BOT_ID'] = self.bot_id
        return await self.b24.call(method, payload)

    def _registration_fields(self) -> dict:
        url = self.s.events_url
        return {
            'EVENT_MESSAGE_ADD': url,
            'EVENT_WELCOME_MESSAGE': url,
            'EVENT_BOT_DELETE': url,
            'OPENLINE': 'Y' if self.s.bot_openline else 'N',
            'PROPERTIES': {
                'NAME': self.s.bot_name,
                'LAST_NAME': self.s.bot_last_name,
                'COLOR': self.s.bot_color,
                'WORK_POSITION': self.s.bot_work_position,
            },
        }

    async def find_existing(self) -> int | None:
        try:
            bots = await self.b24.call('imbot.bot.list', {'CLIENT_ID': self.s.bot_client_id})
        except BitrixError:
            return None
        items = bots.values() if isinstance(bots, dict) else (bots or [])
        for bot in items:
            if str(bot.get('CODE')) == self.s.bot_code:
                return int(bot['ID'])
        return None

    async def register(self) -> int:
        if not self.s.public_url:
            msg = 'imbot v1 работает только в push-режиме: задайте PUBLIC_URL (адрес агента, доступный порталу)'
            raise RuntimeError(msg)
        existing = await self.find_existing()
        if existing:
            self.bot_id = existing
            await self.b24.call('imbot.update', {
                'BOT_ID': existing, 'CLIENT_ID': self.s.bot_client_id,
                'FIELDS': {'CODE': self.s.bot_code, **self._registration_fields()},
            })
            log.info('Бот %s уже зарегистрирован (ID=%s), обработчики обновлены', self.s.bot_code, existing)
        else:
            bot_id = await self.b24.call('imbot.register', {
                'CODE': self.s.bot_code,
                'TYPE': {'supervisor': 'S'}.get(self.s.bot_type, 'B'),
                'CLIENT_ID': self.s.bot_client_id,
                **self._registration_fields(),
            })
            self.bot_id = int(bot_id)
            log.info('Бот зарегистрирован, ID=%s', self.bot_id)
        await self._save()
        return self.bot_id

    async def unregister(self) -> None:
        bot_id = self.bot_id or await self.find_existing()
        if bot_id:
            await self.b24.call('imbot.unregister', {'BOT_ID': bot_id, 'CLIENT_ID': self.s.bot_client_id})
        self.bot_id = None
        await self._save()

    async def send(self, dialog_id: str, text: str, keyboard: list | None = None) -> None:
        chunks = split_text(text) or ['…']
        for i, chunk in enumerate(chunks):
            params: dict = {'DIALOG_ID': dialog_id, 'MESSAGE': chunk}
            if keyboard and i == len(chunks) - 1:
                params['KEYBOARD'] = keyboard
            await self.call('imbot.message.add', params)

    async def typing(self, dialog_id: str) -> None:
        try:
            await self.call('imbot.chat.sendTyping', {'DIALOG_ID': dialog_id})
        except BitrixError as exc:
            log.debug('sendTyping: %s', exc)

    async def create_chat(self, title: str, users: list[int], description: str = '', message: str = '',
                          entity_type: str = '', entity_id: str = '') -> str:
        params = {'TYPE': 'CHAT', 'TITLE': title[:250], 'DESCRIPTION': description, 'COLOR': self.s.bot_color,
                  'USERS': users}
        if entity_type:
            params.update({'ENTITY_TYPE': entity_type, 'ENTITY_ID': entity_id})
        if message:
            params['MESSAGE'] = message
        return f'chat{int(await self.call("imbot.chat.add", params))}'


def make_imbot(b24: Bitrix, settings: Settings, store: Store) -> ImBotBase:
    return ImBotV1(b24, settings, store) if settings.bot_api == 'v1' else ImBotV2(b24, settings, store)
