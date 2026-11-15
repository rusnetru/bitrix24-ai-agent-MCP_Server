"""Клиент OpenAI-совместимого API (LiteLLM proxy → корпоративная модель, например Qwen)."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp

from b24agent.bitrix import make_ssl
from b24agent.config import Settings

log = logging.getLogger(__name__)

_THINK = re.compile(r'<think>.*?</think>', re.DOTALL | re.IGNORECASE)


class LLMError(RuntimeError):
    pass


class ToolsUnsupportedError(LLMError):
    pass


class LLM:
    def __init__(self, settings: Settings):
        if not settings.llm_base_url or not settings.llm_model:
            msg = 'Не заданы LLM_BASE_URL / LLM_MODEL (адрес LiteLLM и имя модели)'
            raise RuntimeError(msg)
        self.s = settings
        base = settings.llm_base_url
        self.url = base if base.endswith('/chat/completions') else f'{base}/chat/completions'
        self._ssl = make_ssl(settings.llm_verify_ssl)
        self._session: aiohttp.ClientSession | None = None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """Возвращает message ответа модели: {'content': str, 'tool_calls': [...]}"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.s.llm_timeout))
        body: dict[str, Any] = {
            'model': self.s.llm_model,
            'messages': messages,
            'temperature': self.s.llm_temperature,
            'max_tokens': self.s.llm_max_tokens,
            **self.s.llm_extra_body,
        }
        if tools:
            body['tools'] = tools
            body['tool_choice'] = 'auto'
        headers = {'Content-Type': 'application/json'}
        if self.s.llm_api_key:
            headers['Authorization'] = f'Bearer {self.s.llm_api_key}'
        async with self._session.post(self.url, json=body, headers=headers, ssl=self._ssl) as resp:
            text = await resp.text()
            if resp.status >= 400:
                low = text.lower()
                if tools and resp.status in (400, 422) and ('tool' in low or 'function' in low):
                    raise ToolsUnsupportedError(text[:500])
                msg = f'LLM HTTP {resp.status}: {text[:500]}'
                raise LLMError(msg)
        try:
            data = json.loads(text)
            msg = data['choices'][0]['message']
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError(f'Неожиданный ответ LLM: {text[:300]}') from exc
        content = msg.get('content') or ''
        if isinstance(content, list):  # некоторые прокси отдают части
            content = ''.join(p.get('text', '') for p in content if isinstance(p, dict))
        msg['content'] = _THINK.sub('', content).strip()
        return msg
