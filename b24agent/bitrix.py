"""Асинхронный клиент REST API Битрикс24 (коробка и облако) через входящий вебхук."""

from __future__ import annotations

import asyncio
import logging
import re
import ssl
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

_RETRY_ERRORS = {'QUERY_LIMIT_EXCEEDED', 'OPERATION_TIME_LIMIT', 'INTERNAL_SERVER_ERROR'}


class BitrixError(RuntimeError):
    def __init__(self, method: str, code: str, description: str):
        super().__init__(f'{method}: {code} — {description}')
        self.method = method
        self.code = code
        self.description = description


def make_ssl(verify: bool | str) -> ssl.SSLContext | bool:
    if verify is False:
        return False
    if isinstance(verify, str):
        return ssl.create_default_context(cafile=verify)
    return True


class Bitrix:
    """Тонкий клиент: ``await b24.call('crm.item.get', {...})``."""

    def __init__(self, webhook_url: str, verify_ssl: bool | str = True, timeout: float = 60):
        self.base = webhook_url.rstrip('/') + '/'
        # REST 3.0: https://host/rest/api/{user}/{code}/{method} (только POST + JSON)
        self.base_v3 = re.sub(r'/rest/(?!api/)', '/rest/api/', self.base, count=1)
        m = re.search(r'/rest/(?:api/)?(\d+)/', self.base)
        self.webhook_user_id = int(m.group(1)) if m else None
        self._ssl = make_ssl(verify_ssl)
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def raw(self, method: str, params: dict | None = None, *, v3: bool = False) -> dict:
        """Вызов метода; возвращает весь ответ (result, next, total). ``v3=True`` — REST 3.0."""
        session = await self._get_session()
        url = f'{self.base_v3}{method}' if v3 else f'{self.base}{method}.json'
        for attempt in range(5):
            try:
                async with session.post(url, json=params or {}, ssl=self._ssl) as resp:
                    try:
                        data = await resp.json(content_type=None)
                    except (aiohttp.ContentTypeError, ValueError):
                        text = await resp.text()
                        raise BitrixError(method, f'HTTP_{resp.status}', text[:300]) from None
            except (aiohttp.ClientConnectionError, TimeoutError) as exc:
                if attempt == 4:
                    raise BitrixError(method, 'CONNECTION', str(exc)) from exc
                await asyncio.sleep(1 + attempt)
                continue
            if isinstance(data, dict) and 'error' in data:
                err = data.get('error')
                if isinstance(err, dict):  # формат ошибок REST 3.0
                    code, desc = str(err.get('code')), str(err.get('message', ''))
                    if err.get('validation'):
                        desc += f' {err["validation"]}'
                else:
                    code, desc = str(err), str(data.get('error_description', ''))
                if code in _RETRY_ERRORS and attempt < 4:
                    await asyncio.sleep(1 + attempt * 2)
                    continue
                raise BitrixError(method, code, desc)
            return data
        raise BitrixError(method, 'RETRY', 'превышено число попыток')

    async def call(self, method: str, params: dict | None = None, *, v3: bool = False) -> Any:
        return (await self.raw(method, params, v3=v3)).get('result')

    async def method_info(self, name: str) -> dict:
        """method.get: {'isExisting': bool, 'isAvailable': bool} — есть ли метод на этой коробке."""
        try:
            res = await self.call('method.get', {'name': name.lower()})
        except BitrixError:
            return {'isExisting': False, 'isAvailable': False}
        return res if isinstance(res, dict) else {'isExisting': False, 'isAvailable': False}

    async def list(
        self,
        method: str,
        params: dict | None = None,
        limit: int = 50,
        key: str | None = None,
    ) -> tuple[list, int | None]:
        """Постраничная выборка (шаг 50). ``key`` — вложенный ключ (items/tasks)."""
        params = dict(params or {})
        out: list = []
        start = int(params.pop('start', 0) or 0)
        total = None
        while len(out) < limit:
            data = await self.raw(method, {**params, 'start': start})
            result = data.get('result')
            if key and isinstance(result, dict):
                result = result.get(key, [])
            if isinstance(result, dict):  # некоторые методы отдают словарь по ID
                result = list(result.values())
            total = data.get('total', total)
            out.extend(result or [])
            nxt = data.get('next')
            if not nxt or not result:
                break
            start = int(nxt)
        return out[:limit], total
