"""Сотрудники (user.*)."""

from __future__ import annotations

from typing import Any

from b24agent.tools.base import (
    LIMIT,
    Toolbox,
    ToolContext,
    arg_int,
    clean,
    schema,
)


def _user_row(u: dict) -> dict:
    return clean({
        'id': int(u['ID']), 'name': ' '.join(filter(None, [u.get('LAST_NAME'), u.get('NAME'), u.get('SECOND_NAME')])),
        'position': u.get('WORK_POSITION'), 'email': u.get('EMAIL'), 'departments': u.get('UF_DEPARTMENT'),
        'active': u.get('ACTIVE'), 'phone': u.get('WORK_PHONE') or u.get('PERSONAL_MOBILE'),
    })


async def _user_search(ctx: ToolContext, args: dict) -> Any:
    q = str(args['query']).strip()
    limit = min(arg_int(args, 'limit', 20) or 20, 100)
    rows, _ = await ctx.b24.list('user.search', {'FIND': q, 'ACTIVE': True}, limit=limit)
    if not rows:
        words = q.split()
        rows, _ = await ctx.b24.list('user.get', {'FILTER': {'%LAST_NAME': words[0], 'ACTIVE': True}}, limit=limit)
    return [_user_row(u) for u in rows if not str(u.get('UF_USER_CRM_ENTITY') or '')]


async def _user_get(ctx: ToolContext, args: dict) -> Any:
    uid = arg_int(args, 'id') or ctx.user_id
    if not uid:
        return {'error': 'Не указан id'}
    users = await ctx.b24.call('user.get', {'ID': uid})
    return _user_row(users[0]) if users else {'error': 'не найден'}


def register(tb: Toolbox) -> None:
    tb.add('user_search', 'Найти сотрудников по ФИО, email или должности. Возвращает ID для назначения ответственных.',
      schema({'query': {'type': 'string'}, 'limit': LIMIT}, ['query']), _user_search)
    tb.add('user_get', 'Данные сотрудника по ID (без id — текущий собеседник).',
      schema({'id': {'type': 'integer'}}), _user_get)
