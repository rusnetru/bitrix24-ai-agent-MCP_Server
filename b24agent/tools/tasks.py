"""Задачи (tasks.task.*, task.commentitem.*)."""

from __future__ import annotations

import re
from typing import Any

from b24agent.tools.base import (
    ENTITY,
    ITEM_ID,
    LIMIT,
    TASK_SELECT,
    Toolbox,
    ToolContext,
    arg_int,
    as_dict,
    clean,
    schema,
    task_row,
)


async def _task_list(ctx: ToolContext, args: dict) -> Any:
    flt = as_dict(args.get('filter'))
    if args.get('entity') and args.get('id'):
        et = await ctx.entities.get(args['entity'])
        flt['UF_CRM_TASK'] = f'{et.task_prefix}_{int(args["id"])}'
    limit = min(arg_int(args, 'limit', 20) or 20, 200)
    tasks, total = await ctx.b24.list('tasks.task.list', {
        'filter': flt, 'select': TASK_SELECT, 'order': as_dict(args.get('order')) or {'ID': 'desc'}},
        limit=limit, key='tasks')
    portal = ctx.settings.portal_url
    rows = []
    for t in tasks:
        row = task_row(t)
        row['url'] = f'{portal}/company/personal/user/0/tasks/task/view/{t.get("id")}/'
        rows.append(row)
    return {'total': total, 'count': len(rows), 'tasks': rows}


async def _task_get(ctx: ToolContext, args: dict) -> Any:
    res = await ctx.b24.call('tasks.task.get', {'taskId': int(args['id']), 'select': [
        *TASK_SELECT, 'DESCRIPTION', 'AUDITORS', 'ACCOMPLICES', 'TAGS', 'CHECKLIST', 'TIME_ESTIMATE']})
    t = res.get('task', res)
    row = task_row(t)
    row['description'] = (t.get('description') or '')[:4000]
    row['auditors'] = t.get('auditors')
    row['accomplices'] = t.get('accomplices')
    row['creator'] = (t.get('creator') or {}).get('name')
    if t.get('checklist'):
        row['checklist'] = [
            {'title': c.get('title'), 'done': c.get('isComplete') in ('Y', True)}
            for c in (t['checklist'].values() if isinstance(t['checklist'], dict) else t['checklist'])]
    crm = []
    for b in t.get('ufCrmTask') or []:
        parsed = await ctx.entities.from_task_binding(b)
        if parsed:
            crm.append({'entity': parsed[0].title, 'entityTypeId': parsed[0].id, 'id': parsed[1]})
    row['crm'] = crm
    row['url'] = f'{ctx.settings.portal_url}/company/personal/user/0/tasks/task/view/{t.get("id")}/'
    return clean(row)


async def _task_comments(ctx: ToolContext, args: dict) -> Any:
    res = await ctx.b24.call('task.commentitem.getlist', {'TASKID': int(args['id']), 'ORDER': {'ID': 'desc'}})
    limit = min(arg_int(args, 'limit', 20) or 20, 100)
    return [{'id': c.get('ID'), 'author': c.get('AUTHOR_NAME'), 'date': c.get('POST_DATE'),
             'text': re.sub(r'\s+', ' ', c.get('POST_MESSAGE') or '')[:2000]} for c in (res or [])[:limit]]


async def _task_add(ctx: ToolContext, args: dict) -> Any:
    fields: dict[str, Any] = {'TITLE': args['title']}
    if args.get('description'):
        fields['DESCRIPTION'] = args['description']
    responsible = arg_int(args, 'responsible_id') or ctx.user_id
    if not responsible:
        return {'error': 'Укажите responsible_id исполнителя'}
    fields['RESPONSIBLE_ID'] = responsible
    if ctx.user_id:
        fields['CREATED_BY'] = ctx.user_id
    if args.get('deadline'):
        fields['DEADLINE'] = args['deadline']
    if args.get('priority') is not None:
        fields['PRIORITY'] = int(args['priority'])
    if args.get('auditors'):
        fields['AUDITORS'] = [int(x) for x in args['auditors']]
    if args.get('group_id'):
        fields['GROUP_ID'] = int(args['group_id'])
    if args.get('entity') and args.get('crm_id'):
        et = await ctx.entities.get(args['entity'])
        fields['UF_CRM_TASK'] = [f'{et.task_prefix}_{int(args["crm_id"])}']
    res = await ctx.b24.call('tasks.task.add', {'fields': fields})
    tid = (res.get('task') or {}).get('id')
    return {'ok': True, 'id': tid, 'url': f'{ctx.settings.portal_url}/company/personal/user/0/tasks/task/view/{tid}/'}


async def _task_update(ctx: ToolContext, args: dict) -> Any:
    tid = int(args['id'])
    fields = as_dict(args.get('fields'))
    if fields:
        await ctx.b24.call('tasks.task.update', {'taskId': tid, 'fields': fields})
    action = (args.get('action') or '').lower()
    if action:
        method = {'complete': 'tasks.task.complete', 'renew': 'tasks.task.renew', 'start': 'tasks.task.start',
                  'defer': 'tasks.task.defer', 'pause': 'tasks.task.pause'}.get(action)
        if not method:
            return {'error': f'Неизвестное действие {action}'}
        await ctx.b24.call(method, {'taskId': tid})
    return {'ok': True, 'id': tid}


async def _task_comment_add(ctx: ToolContext, args: dict) -> Any:
    fields = {'POST_MESSAGE': str(args['text'])}
    cid = await ctx.b24.call('task.commentitem.add', {'TASKID': int(args['id']), 'FIELDS': fields})
    return {'ok': True, 'comment_id': cid}


def register(tb: Toolbox) -> None:
    tb.add('task_list', 'Список задач. Фильтр — поля в верхнем регистре: {"RESPONSIBLE_ID": 5, "REAL_STATUS": [2,3], '
      '"%TITLE": "отчёт", "<DEADLINE": "2026-12-01", "GROUP_ID": 3}. Статусы: 2 ждёт, 3 выполняется, '
      '4 ждёт контроля, 5 завершена, 6 отложена. Задачи объекта CRM: entity + id.',
      schema({'filter': {'type': 'object'}, 'entity': ENTITY, 'id': ITEM_ID, 'limit': LIMIT,
               'order': {'type': 'object'}}), _task_list)
    tb.add('task_get', 'Подробности задачи: описание, участники, привязки к CRM, чек-лист.',
      schema({'id': {'type': 'integer'}}, ['id']), _task_get)
    tb.add('task_comments', 'Комментарии к задаче.',
      schema({'id': {'type': 'integer'}, 'limit': LIMIT}, ['id']), _task_comments)
    tb.add('task_add', 'Создать задачу. deadline — ISO 8601 (2026-11-20T18:00:00+03:00). '
      'Можно привязать к объекту CRM через entity + crm_id.',
      schema({
          'title': {'type': 'string'}, 'description': {'type': 'string'},
          'responsible_id': {'type': 'integer', 'description': 'ID исполнителя (по умолчанию — автор запроса)'},
          'deadline': {'type': 'string'}, 'priority': {'type': 'integer', 'description': '2 — высокий, 1 — обычный'},
          'auditors': {'type': 'array', 'items': {'type': 'integer'}},
          'group_id': {'type': 'integer'}, 'entity': ENTITY, 'crm_id': ITEM_ID,
      }, ['title']), _task_add, write=True)
    tb.add('task_update', 'Изменить задачу. fields — поля в верхнем регистре (TITLE, DEADLINE, RESPONSIBLE_ID, '
      'DESCRIPTION, PRIORITY). action: complete | renew | start | defer — сменить статус.',
      schema({'id': {'type': 'integer'}, 'fields': {'type': 'object'}, 'action': {'type': 'string'}}, ['id']),
      _task_update, write=True)
    tb.add('task_comment_add', 'Добавить комментарий к задаче.',
      schema({'id': {'type': 'integer'}, 'text': {'type': 'string'}}, ['id', 'text']),
      _task_comment_add, write=True)
