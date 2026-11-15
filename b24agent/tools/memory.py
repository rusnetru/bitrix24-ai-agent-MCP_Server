"""Личная память ассистента: предпочтения и особенности работы сотрудника.

Доступна только личному ассистенту (``personal_only``) и только про его владельца. Хранится локально
в SQLite агента (``DATA_DIR``), в Битрикс24 не пишется. Содержимое памяти подставляется в системный
промпт каждого разговора с владельцем.
"""

from __future__ import annotations

import re
from typing import Any

from b24agent.tools.base import Toolbox, ToolContext, arg_int, schema

MAX_NOTE = 500
_SECRET = re.compile(r'(парол|password|token|токен|/rest/\d+/[a-z0-9]{8,}|sk-[a-z0-9]{10,})', re.IGNORECASE)


async def _save(ctx: ToolContext, args: dict) -> Any:
    text = re.sub(r'\s+', ' ', str(args.get('text') or '')).strip()
    if not text:
        return {'error': 'Пустая заметка'}
    if _SECRET.search(text):
        return {'error': 'Похоже на пароль или токен — такое в память не сохраняется'}
    topic = str(args.get('topic') or '').strip()[:40]
    uid = ctx.profile.user_id
    for mid, _topic, old in await ctx.store.memories(uid):  # не дублировать одно и то же
        if old.lower() == text.lower():
            return {'ok': True, 'id': mid, 'duplicate': True}
    mid = await ctx.store.add_memory(uid, topic, text[:MAX_NOTE], ctx.settings.personal_memory_max)
    return {'ok': True, 'id': mid}


async def _list(ctx: ToolContext, args: dict) -> Any:
    return [{'id': i, 'topic': t, 'text': x} for i, t, x in await ctx.store.memories(ctx.profile.user_id)]


async def _forget(ctx: ToolContext, args: dict) -> Any:
    uid = ctx.profile.user_id
    if args.get('all') in (True, 'true', 1):
        return {'ok': True, 'deleted': await ctx.store.forget_memory(uid)}
    mid = arg_int(args, 'id')
    if not mid:
        return {'error': 'Укажите id заметки (см. memory_list) или all=true'}
    return {'ok': True, 'deleted': await ctx.store.forget_memory(uid, mid)}


def register(tb: Toolbox) -> None:
    tb.add('memory_save', 'Запомнить устойчивое предпочтение или важный факт о пользователе (стиль общения, '
           'роль, ключевые клиенты и проекты, коллеги, привычные сроки). topic — короткая тема: «стиль», '
           '«клиенты», «команда». Не сохраняй пароли и токены.',
           schema({'text': {'type': 'string'}, 'topic': {'type': 'string'}}, ['text']), _save, personal_only=True)
    tb.add('memory_list', 'Что сохранено в личной памяти о пользователе.', schema({}), _list, personal_only=True)
    tb.add('memory_forget', 'Удалить заметку из личной памяти по id или всё (all=true) — по просьбе пользователя.',
           schema({'id': {'type': 'integer'}, 'all': {'type': 'boolean'}}), _forget, personal_only=True)
