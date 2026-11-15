"""Работа с чат-ботом (imbot.*), зарегистрированным через входящий вебхук.

В режиме вебхука каждый вызов imbot.* обязан содержать ``CLIENT_ID`` —
произвольную строку, которая связывает бота с этим вебхуком.
"""

from __future__ import annotations

import logging
from typing import Any

from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.store import Store

log = logging.getLogger(__name__)

MAX_MESSAGE = 9000


class ImBot:
    def __init__(self, b24: Bitrix, settings: Settings, store: Store):
        self.b24 = b24
        self.s = settings
        self.store = store
        self.bot_id: int | None = None

    async def load(self) -> int | None:
        self.bot_id = await self.store.get('bot_id')
        return self.bot_id

    async def call(self, method: str, params: dict | None = None) -> Any:
        payload = {'CLIENT_ID': self.s.bot_client_id, **(params or {})}
        if self.bot_id and 'BOT_ID' not in payload:
            payload['BOT_ID'] = self.bot_id
        return await self.b24.call(method, payload)

    # --- регистрация ---
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
            msg = 'Не задан PUBLIC_URL — адрес этого сервиса, доступный с сервера Битрикс24'
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
                'TYPE': 'B',
                'CLIENT_ID': self.s.bot_client_id,
                **self._registration_fields(),
            })
            self.bot_id = int(bot_id)
            log.info('Бот зарегистрирован, ID=%s', self.bot_id)
        await self.store.set('bot_id', self.bot_id)
        return self.bot_id

    async def unregister(self) -> None:
        bot_id = self.bot_id or await self.find_existing()
        if bot_id:
            await self.b24.call('imbot.unregister', {'BOT_ID': bot_id, 'CLIENT_ID': self.s.bot_client_id})
        await self.store.set('bot_id', None)
        self.bot_id = None

    # --- сообщения ---
    async def send(self, dialog_id: str, text: str, attach: list | None = None) -> None:
        chunks = _split(text, MAX_MESSAGE) or ['…']
        for i, chunk in enumerate(chunks):
            params: dict = {'DIALOG_ID': dialog_id, 'MESSAGE': chunk}
            if attach and i == len(chunks) - 1:
                params['ATTACH'] = attach
            await self.call('imbot.message.add', params)

    async def typing(self, dialog_id: str) -> None:
        try:
            await self.call('imbot.chat.sendTyping', {'DIALOG_ID': dialog_id})
        except BitrixError as exc:
            log.debug('sendTyping: %s', exc)

    async def create_chat(
        self,
        title: str,
        users: list[int],
        entity_type: str,
        entity_id: str,
        description: str = '',
        message: str = '',
    ) -> int:
        params = {
            'TYPE': 'CHAT',
            'TITLE': title[:250],
            'DESCRIPTION': description,
            'COLOR': self.s.bot_color,
            'USERS': users,
            'ENTITY_TYPE': entity_type,
            'ENTITY_ID': entity_id,
        }
        if message:
            params['MESSAGE'] = message
        return int(await self.call('imbot.chat.add', params))


def _split(text: str, size: int) -> list[str]:
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
