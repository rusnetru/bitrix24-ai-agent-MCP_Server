"""Бизнес-процессы (задания на согласование), универсальные списки, дела CRM и история стадий."""

from __future__ import annotations

import re
from typing import Any

from b24agent.tools.base import (
    ENTITY,
    ITEM_ID,
    LIMIT,
    Toolbox,
    ToolContext,
    arg_int,
    as_dict,
    clean,
    schema,
)

BP_STATUS = {'0': 'в работе', '1': 'одобрено', '2': 'отклонено', '3': 'выполнено', '4': 'просрочено'}


# ---------------------------------------------------------------- бизнес-процессы
async def _bp_tasks(ctx: ToolContext, args: dict) -> Any:
    user = arg_int(args, 'user_id') or ctx.user_id
    flt: dict = {'STATUS': 0}
    if user:
        flt['USER_ID'] = user
    rows, total = await ctx.b24.list('bizproc.task.list', {
        'SELECT': ['ID', 'WORKFLOW_ID', 'NAME', 'DESCRIPTION', 'DOCUMENT_NAME', 'DOCUMENT_URL', 'STATUS',
                   'USER_STATUS', 'ACTIVITY', 'MODIFIED', 'PARAMETERS'],
        'FILTER': flt, 'ORDER': {'ID': 'DESC'}}, limit=min(arg_int(args, 'limit', 20) or 20, 100))
    out = []
    for r in rows:
        row = clean({k.lower(): v for k, v in r.items() if k != 'PARAMETERS'})
        if row.get('description'):
            row['description'] = re.sub(r'<[^>]+>', ' ', row['description'])[:1000]
        fields = ((r.get('PARAMETERS') or {}).get('Fields')) or []
        if fields:
            row['input_fields'] = fields
        url = row.get('document_url')
        if url and url.startswith('/'):
            row['document_url'] = ctx.settings.portal_url + url
        row['status'] = BP_STATUS.get(str(r.get('STATUS')), r.get('STATUS'))
        out.append(row)
    return {'total': total, 'tasks': out}


async def _bp_task_complete(ctx: ToolContext, args: dict) -> Any:
    status = str(args.get('decision', '')).lower()
    code = {'approve': 1, 'yes': 1, 'reject': 2, 'no': 2, 'ok': 3, 'cancel': 4}.get(status)
    if code is None:
        return {'error': 'decision: approve | reject | ok | cancel'}
    params: dict = {'TASK_ID': int(args['task_id']), 'STATUS': code}
    if args.get('comment'):
        params['COMMENT'] = args['comment']
    if args.get('fields'):
        params['FIELDS'] = as_dict(args['fields'])
    await ctx.b24.call('bizproc.task.complete', params)
    return {'ok': True}


async def _bp_start(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    docs = {
        1: ['crm', 'CCrmDocumentLead', f'LEAD_{item_id}'], 2: ['crm', 'CCrmDocumentDeal', f'DEAL_{item_id}'],
        3: ['crm', 'CCrmDocumentContact', f'CONTACT_{item_id}'],
        4: ['crm', 'CCrmDocumentCompany', f'COMPANY_{item_id}'],
    }
    doc = docs.get(et.id) or ['crm', 'Bitrix\\Crm\\Integration\\BizProc\\Document\\Dynamic', f'DYNAMIC_{et.id}_{item_id}']
    wid = await ctx.b24.call('bizproc.workflow.start', {
        'TEMPLATE_ID': int(args['template_id']), 'DOCUMENT_ID': doc, 'PARAMETERS': as_dict(args.get('parameters'))})
    return {'ok': True, 'workflow_id': wid}


async def _bp_templates(ctx: ToolContext, args: dict) -> Any:
    rows, _ = await ctx.b24.list('bizproc.workflow.template.list', {
        'SELECT': ['ID', 'NAME', 'MODULE_ID', 'ENTITY', 'DOCUMENT_TYPE', 'AUTO_EXECUTE', 'PARAMETERS']}, limit=500)
    if args.get('entity'):
        code = (await ctx.entities.get(args['entity'])).code
        rows = [r for r in rows if (r.get('DOCUMENT_TYPE') or [None, None, None])[-1] == code]
    return rows


# ---------------------------------------------------------------- универсальные списки
async def _lists(ctx: ToolContext, args: dict) -> Any:
    params: dict = {'IBLOCK_TYPE_ID': args.get('iblock_type') or 'lists'}
    if args.get('group_id'):
        params.update({'IBLOCK_TYPE_ID': 'lists_socnet', 'SOCNET_GROUP_ID': int(args['group_id'])})
    rows, _ = await ctx.b24.list('lists.get', params, limit=200)
    return [clean({'id': r.get('ID'), 'code': r.get('CODE'), 'name': r.get('NAME'),
                   'description': r.get('DESCRIPTION')}) for r in rows]


async def _list_fields(ctx: ToolContext, args: dict) -> Any:
    res = await ctx.b24.call('lists.field.get', {'IBLOCK_TYPE_ID': args.get('iblock_type') or 'lists',
                                                'IBLOCK_ID': int(args['list_id'])})
    items = res.values() if isinstance(res, dict) else (res or [])
    return [clean({'code': f.get('FIELD_ID'), 'name': f.get('NAME'), 'type': f.get('TYPE'),
                   'required': f.get('IS_REQUIRED') == 'Y', 'multiple': f.get('MULTIPLE') == 'Y',
                   'values': f.get('DISPLAY_VALUES_FORM')}) for f in items]


async def _list_elements(ctx: ToolContext, args: dict) -> Any:
    params: dict = {'IBLOCK_TYPE_ID': args.get('iblock_type') or 'lists', 'IBLOCK_ID': int(args['list_id'])}
    flt = as_dict(args.get('filter'))
    if flt:
        params['FILTER'] = flt
    if args.get('element_id'):
        params['ELEMENT_ID'] = int(args['element_id'])
    rows, total = await ctx.b24.list('lists.element.get', params, limit=min(arg_int(args, 'limit', 20) or 20, 200))
    return {'total': total, 'elements': [clean(r) for r in rows]}


async def _list_element_add(ctx: ToolContext, args: dict) -> Any:
    fields = as_dict(args.get('fields'))
    code = args.get('element_code') or f'agent_{abs(hash(str(fields))) % 10**10}'
    eid = await ctx.b24.call('lists.element.add', {
        'IBLOCK_TYPE_ID': args.get('iblock_type') or 'lists', 'IBLOCK_ID': int(args['list_id']),
        'ELEMENT_CODE': code, 'FIELDS': fields})
    return {'ok': True, 'id': eid}


# ---------------------------------------------------------------- дела и история CRM
async def _todo_add(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    params: dict = {'ownerTypeId': et.id, 'ownerId': int(args['id']), 'deadline': args['deadline'],
                    'title': args.get('title', ''), 'description': args.get('description', '')}
    responsible = arg_int(args, 'responsible_id') or ctx.user_id
    if responsible:
        params['responsibleId'] = responsible
    res = await ctx.b24.call('crm.activity.todo.add', params)
    return {'ok': True, 'id': (res or {}).get('id') if isinstance(res, dict) else res}


async def _stage_history(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    rows, _ = await ctx.b24.list('crm.stagehistory.list', {
        'entityTypeId': et.id, 'filter': {'OWNER_ID': int(args['id'])}, 'order': {'ID': 'ASC'},
        'select': ['ID', 'TYPE_ID', 'CREATED_TIME', 'CATEGORY_ID', 'STAGE_ID', 'STAGE_SEMANTIC_ID']},
        limit=200, key='items')
    stages: dict = {}
    for r in rows:
        if r.get('STAGE_ID') and r['STAGE_ID'] not in stages:
            stages.update(await ctx.tb.stages(ctx, et, r.get('CATEGORY_ID')))
        r['STAGE_NAME'] = (stages.get(r.get('STAGE_ID')) or {}).get('name')
    return rows


def register(tb: Toolbox) -> None:
    tb.add('bp_tasks', 'Задания бизнес-процессов на согласование/ознакомление (bizproc.task.list) — по умолчанию '
           'открытые задания собеседника.', schema({'user_id': {'type': 'integer'}, 'limit': LIMIT}), _bp_tasks)
    tb.add('bp_task_complete', 'Выполнить задание бизнес-процесса от имени пользователя вебхука: '
           'decision approve | reject | ok | cancel, comment, fields — значения полей запроса.',
           schema({'task_id': {'type': 'integer'}, 'decision': {'type': 'string'}, 'comment': {'type': 'string'},
                   'fields': {'type': 'object'}}, ['task_id', 'decision']), _bp_task_complete, write=True)
    tb.add('bp_templates', 'Шаблоны бизнес-процессов (нужны права администратора у пользователя вебхука).',
           schema({'entity': ENTITY}), _bp_templates)
    tb.add('bp_start', 'Запустить бизнес-процесс по шаблону для элемента CRM/смарт-процесса '
           '(нужны права администратора).',
           schema({'template_id': {'type': 'integer'}, 'entity': ENTITY, 'id': ITEM_ID,
                   'parameters': {'type': 'object'}}, ['template_id', 'entity', 'id']), _bp_start, write=True)
    tb.add('lists_get', 'Универсальные списки портала (lists.get). group_id — списки рабочей группы.',
           schema({'iblock_type': {'type': 'string'}, 'group_id': {'type': 'integer'}}), _lists)
    tb.add('list_fields', 'Поля универсального списка (коды PROPERTY_xxx, типы, варианты).',
           schema({'list_id': {'type': 'integer'}, 'iblock_type': {'type': 'string'}}, ['list_id']), _list_fields)
    tb.add('list_elements', 'Элементы универсального списка. filter — {"NAME": "...", "PROPERTY_123": "..."}.',
           schema({'list_id': {'type': 'integer'}, 'filter': {'type': 'object'}, 'element_id': {'type': 'integer'},
                   'iblock_type': {'type': 'string'}, 'limit': LIMIT}, ['list_id']), _list_elements)
    tb.add('list_element_add', 'Добавить элемент в универсальный список. fields: {"NAME": ..., "PROPERTY_123": ...}.',
           schema({'list_id': {'type': 'integer'}, 'fields': {'type': 'object'}, 'element_code': {'type': 'string'},
                   'iblock_type': {'type': 'string'}}, ['list_id', 'fields']), _list_element_add, write=True)
    tb.add('crm_todo_add', 'Создать дело (todo) в элементе CRM/смарт-процесса: deadline ISO 8601, title, '
           'description, responsible_id.',
           schema({'entity': ENTITY, 'id': ITEM_ID, 'deadline': {'type': 'string'}, 'title': {'type': 'string'},
                   'description': {'type': 'string'}, 'responsible_id': {'type': 'integer'}},
                  ['entity', 'id', 'deadline']), _todo_add, write=True)
    tb.add('crm_stage_history', 'История движения элемента по стадиям (лиды, сделки, счета, смарт-процессы).',
           schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), _stage_history)
