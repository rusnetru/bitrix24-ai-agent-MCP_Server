"""Инструменты, доступные только из разговора с ботом: привязка чатов и таймлайна к объектам."""

from __future__ import annotations

import logging
from typing import Any

from b24agent.bitrix import BitrixError
from b24agent.tools.base import (
    ENTITY,
    ITEM_ID,
    Toolbox,
    ToolContext,
    schema,
)

log = logging.getLogger(__name__)


async def _chat_bind(ctx: ToolContext, args: dict) -> Any:
    bot = ctx.bot
    if not bot or not bot.dialog_id:
        return {'error': 'Нет текущего чата'}
    if not args.get('entity') or not args.get('id'):
        await ctx.store.unbind_chat(bot.dialog_id)
        bot.bound = None
        return {'ok': True, 'unbound': True}
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id})  # проверка существования
    await ctx.store.bind_chat(bot.dialog_id, et.id, item_id, ctx.user_id)
    bot.bound = (et.id, item_id)
    return {'ok': True, 'bound_to': f'{et.title} #{item_id}', 'url': et.url(ctx.settings.portal_url, item_id)}


async def _chat_create(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
    users = {int(x) for x in args.get('user_ids') or []}
    if ctx.user_id:
        users.add(ctx.user_id)
    if item.get('assignedById'):
        users.add(int(item['assignedById']))
    title = item.get('title') or ' '.join(filter(None, [item.get('lastName'), item.get('name')])) or f'#{item_id}'
    url = et.url(ctx.settings.portal_url, item_id)
    dialog = await ctx.bot.imbot.create_chat(
        title=f'ИИ: {et.title} «{title}»',
        users=sorted(users),
        description=url,
        message=f'Чат по объекту [url={url}]{et.title} «{title}»[/url]. '
                f'Пишите вопросы — я работаю в контексте этого объекта.',
        entity_type='B24AGENT',
        entity_id=f'{et.id}|{item_id}',
    )
    await ctx.store.bind_chat(dialog, et.id, item_id, ctx.user_id)
    chat_url = f'{ctx.settings.portal_url}/online/?IM_DIALOG={dialog}'
    fields = {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type,
              'COMMENT': f'Создан чат с ИИ-агентом: {chat_url}'}
    try:
        await ctx.b24.call('crm.timeline.comment.add', {'fields': fields})
    except BitrixError as exc:
        log.warning('Не удалось оставить ссылку на чат в таймлайне: %s', exc)
    return {'ok': True, 'dialog_id': dialog, 'chat_url': chat_url}


async def _timeline_watch(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    if args.get('enabled', True) in (False, 'false', 0, 'N'):
        await ctx.store.unwatch(et.id, item_id)
        return {'ok': True, 'watching': False}
    last, _ = await ctx.b24.list('crm.timeline.comment.list', {
        'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type}, 'select': ['ID'],
        'order': {'ID': 'DESC'}}, limit=1)
    last_id = max((int(c['ID']) for c in last), default=0)
    await ctx.store.watch(et.id, item_id, last_id, ctx.user_id)
    return {'ok': True, 'watching': True, 'entity': f'{et.title} #{item_id}'}


def register(tb: Toolbox) -> None:
    tb.add('chat_bind_entity', 'Привязать ТЕКУЩИЙ чат к элементу CRM/смарт-процесса: агент будет работать в '
      'контексте этого объекта. Без параметров — отвязать.',
      schema({'entity': ENTITY, 'id': ITEM_ID}), _chat_bind, bot_only=True)
    tb.add('chat_create_for_entity', 'Создать отдельный групповой чат с ИИ-агентом по элементу CRM/смарт-процесса '
      '(участники: ответственный, автор запроса и user_ids) и оставить ссылку в таймлайне.',
      schema({'entity': ENTITY, 'id': ITEM_ID, 'user_ids': {'type': 'array', 'items': {'type': 'integer'}}},
              ['entity', 'id']), _chat_create, write=True, bot_only=True)
    tb.add('timeline_watch', 'Включить/выключить ответы агента на комментарии в таймлайне элемента CRM. '
      'После включения агент отвечает на комментарии этого объекта.',
      schema({'entity': ENTITY, 'id': ITEM_ID, 'enabled': {'type': 'boolean'}}, ['entity', 'id']),
      _timeline_watch, write=True, bot_only=True)
