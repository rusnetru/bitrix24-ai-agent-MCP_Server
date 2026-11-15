"""CRM: стандартные объекты и смарт-процессы (crm.item.*, crm.type.*, crm.status.*, таймлайн)."""

from __future__ import annotations

import re
from typing import Any

from b24agent.bitrix import BitrixError
from b24agent.entities import EntityType
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
    to_int,
)


async def _entity_types(ctx: ToolContext, args: dict) -> Any:
    return [{'entityTypeId': e.id, 'code': e.code, 'title': e.title, 'smart_process': e.is_dynamic,
             'has_pipelines': e.categories_enabled and e.stages_enabled}
            for e in await ctx.entities.all()]


async def _fields(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    out = []
    for key, m in (await ctx.tb.fields(ctx, et)).items():
        if args.get('only_custom') and not key.startswith('uf'):
            continue
        row = {'code': key, 'title': m.get('title'), 'type': m.get('type')}
        if m.get('isRequired'):
            row['required'] = True
        if m.get('isReadOnly'):
            row['readonly'] = True
        if m.get('isMultiple'):
            row['multiple'] = True
        if m.get('items'):
            row['values'] = {str(i.get('ID')): i.get('VALUE') for i in m['items']}
        out.append(row)
    return {'entity': et.title, 'entityTypeId': et.id, 'fields': out}


async def _pipelines(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    if not et.stages_enabled:
        return {'error': f'У типа «{et.title}» нет стадий'}
    cats = [{'id': 0, 'name': 'Основная'}]
    if et.categories_enabled:
        res = await ctx.b24.call('crm.category.list', {'entityTypeId': et.id})
        cats = [{'id': int(c['id']), 'name': c.get('name'), 'default': c.get('isDefault') in ('Y', True)}
                for c in (res or {}).get('categories', [])] or cats
    for cat in cats:
        st = await ctx.tb.stages(ctx, et, cat['id'])
        cat['stages'] = [{'stageId': k, **v} for k, v in st.items()]
    return {'entity': et.title, 'entityTypeId': et.id, 'pipelines': cats}


def _default_select(et: EntityType) -> list[str]:
    if et.id == 3:
        return ['id', 'name', 'lastName', 'secondName', 'post', 'companyId', 'assignedById', 'createdTime']
    if et.id == 4:
        return ['id', 'title', 'companyType', 'industry', 'assignedById', 'createdTime']
    return ['id', 'title', 'stageId', 'categoryId', 'opportunity', 'currencyId', 'assignedById',
            'companyId', 'contactId', 'createdTime', 'updatedTime']


async def _list(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    limit = min(arg_int(args, 'limit', 20) or 20, 200)
    params = {
        'entityTypeId': et.id,
        'filter': as_dict(args.get('filter')),
        'select': args.get('select') or _default_select(et),
        'order': as_dict(args.get('order')) or {'id': 'desc'},
    }
    items, total = await ctx.b24.list('crm.item.list', params, limit=limit, key='items')
    stages = {}
    if et.stages_enabled:
        for cat in {i.get('categoryId') for i in items if i.get('stageId')}:
            stages.update(await ctx.tb.stages(ctx, et, cat))
    rows = []
    for it in items:
        row = clean(it)
        if it.get('stageId') in stages:
            row['stageName'] = stages[it['stageId']]['name']
        row['url'] = et.url(ctx.settings.portal_url, int(it['id']))
        rows.append(row)
    return {'entity': et.title, 'entityTypeId': et.id, 'total': total, 'count': len(rows), 'items': rows}


async def _search(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    q = str(args['query']).strip()
    limit = min(arg_int(args, 'limit', 20) or 20, 100)
    found: dict[int, dict] = {}
    if et.id in (1, 3, 4):
        legacy = {1: 'lead', 3: 'contact', 4: 'company'}[et.id]
        comm = None
        if '@' in q:
            comm = 'EMAIL'
        elif re.fullmatch(r'[\d\s()+\-]{5,}', q):
            comm = 'PHONE'
        if comm:
            try:
                dup = await ctx.b24.call('crm.duplicate.findbycomm', {
                    'type': comm, 'values': [q], 'entity_type': et.code})
                for iid in (dup or {}).get(et.code, []):
                    found[int(iid)] = {'id': int(iid)}
            except BitrixError:
                pass
        if not comm or not found:
            select = ['ID', 'TITLE', 'NAME', 'LAST_NAME', 'SECOND_NAME', 'COMPANY_TITLE', 'PHONE', 'EMAIL',
                      'ASSIGNED_BY_ID']
            filters = [{'%TITLE': q}] if et.id in (1, 4) else []
            if et.id in (1, 3):
                words = q.split()
                filters += [{'%LAST_NAME': words[0]}, {'%NAME': words[0]}]
                if len(words) > 1:
                    filters.insert(0, {'%LAST_NAME': words[0], '%NAME': words[1]})
                    filters.insert(0, {'%NAME': words[0], '%LAST_NAME': words[1]})
            for flt in filters:
                rows, _ = await ctx.b24.list(f'crm.{legacy}.list', {'filter': flt, 'select': select}, limit=limit)
                for r in rows:
                    found.setdefault(int(r['ID']), clean({
                        'id': int(r['ID']), 'title': r.get('TITLE'),
                        'name': ' '.join(filter(None, [r.get('LAST_NAME'), r.get('NAME'), r.get('SECOND_NAME')])),
                        'company': r.get('COMPANY_TITLE'),
                        'phone': [p.get('VALUE') for p in r.get('PHONE') or []],
                        'email': [p.get('VALUE') for p in r.get('EMAIL') or []],
                        'assignedById': r.get('ASSIGNED_BY_ID'),
                    }))
                if len(found) >= limit:
                    break
    else:
        items, _ = await ctx.b24.list('crm.item.list', {
            'entityTypeId': et.id, 'filter': {'%title': q},
            'select': _default_select(et), 'order': {'id': 'desc'}}, limit=limit, key='items')
        if not items and q.isdigit():
            items, _ = await ctx.b24.list('crm.item.list', {
                'entityTypeId': et.id, 'filter': {'id': int(q)}, 'select': _default_select(et)},
                limit=1, key='items')
        for it in items:
            found[int(it['id'])] = clean(it)
    for iid, row in found.items():
        row['url'] = et.url(ctx.settings.portal_url, iid)
    return {'entity': et.title, 'entityTypeId': et.id, 'items': list(found.values())[:limit]}


async def _get(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = arg_int(args, 'id')
    item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
    out = {'entity': et.title, 'entityTypeId': et.id, **(await ctx.tb.humanize(ctx, et, item))}
    if et.id in (1, 3, 4):
        legacy = {1: 'lead', 3: 'contact', 4: 'company'}[et.id]
        try:
            raw = await ctx.b24.call(f'crm.{legacy}.get', {'id': item_id})
            for key in ('PHONE', 'EMAIL', 'WEB', 'IM'):
                if raw.get(key):
                    out[key.lower()] = [f'{v.get("VALUE")} ({v.get("VALUE_TYPE")})' for v in raw[key]]
        except BitrixError:
            pass
    for key in ('assignedById', 'createdBy', 'updatedBy'):
        if item.get(key):
            out[f'{key}_name'] = await ctx.tb.user_name(ctx, int(item[key]))
    return out


async def _timeline(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = arg_int(args, 'id')
    limit = min(arg_int(args, 'limit', 20) or 20, 100)
    comments, _ = await ctx.b24.list('crm.timeline.comment.list', {
        'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type},
        'select': ['ID', 'CREATED', 'AUTHOR_ID', 'COMMENT'],
        'order': {'ID': 'DESC'},
    }, limit=limit)
    acts: list = []
    try:
        acts, _ = await ctx.b24.list('crm.activity.list', {
            'filter': {'OWNER_TYPE_ID': et.id, 'OWNER_ID': item_id},
            'select': ['ID', 'SUBJECT', 'TYPE_ID', 'PROVIDER_ID', 'DIRECTION', 'CREATED', 'DEADLINE',
                       'COMPLETED', 'RESPONSIBLE_ID', 'DESCRIPTION'],
            'order': {'ID': 'DESC'},
        }, limit=limit)
    except BitrixError:
        pass
    for c in comments:
        c['AUTHOR'] = await ctx.tb.user_name(ctx, to_int(c.get('AUTHOR_ID')))
    for act in acts:
        if act.get('DESCRIPTION'):
            act['DESCRIPTION'] = re.sub(r'<[^>]+>', ' ', act['DESCRIPTION'])[:500]
    return {'comments': comments, 'activities': [clean(x) for x in acts]}


async def _related(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
    out: dict[str, Any] = {}
    contact_ids = item.get('contactIds') or ([item['contactId']] if item.get('contactId') else [])
    if contact_ids:
        rows, _ = await ctx.b24.list('crm.contact.list', {
            'filter': {'ID': contact_ids}, 'select': ['ID', 'NAME', 'LAST_NAME', 'POST', 'PHONE', 'EMAIL']},
            limit=50)
        out['contacts'] = [clean({
            'id': r['ID'], 'name': ' '.join(filter(None, [r.get('LAST_NAME'), r.get('NAME')])),
            'post': r.get('POST'), 'phone': [p['VALUE'] for p in r.get('PHONE') or []],
            'email': [p['VALUE'] for p in r.get('EMAIL') or []]}) for r in rows]
    if item.get('companyId'):
        comp = await ctx.b24.call('crm.company.get', {'id': item['companyId']})
        out['company'] = {'id': comp.get('ID'), 'title': comp.get('TITLE')}
    tasks, _ = await ctx.b24.list('tasks.task.list', {
        'filter': {'UF_CRM_TASK': f'{et.task_prefix}_{item_id}'}, 'select': TASK_SELECT}, limit=50, key='tasks')
    out['tasks'] = [task_row(t) for t in tasks]
    return out


async def _set_comms(ctx: ToolContext, et: EntityType, item_id: int, args: dict) -> None:
    if et.id not in (1, 3, 4) or not (args.get('phone') or args.get('email')):
        return
    legacy = {1: 'lead', 3: 'contact', 4: 'company'}[et.id]
    fields = {}
    if args.get('phone'):
        fields['PHONE'] = [{'VALUE': args['phone'], 'VALUE_TYPE': 'WORK'}]
    if args.get('email'):
        fields['EMAIL'] = [{'VALUE': args['email'], 'VALUE_TYPE': 'WORK'}]
    await ctx.b24.call(f'crm.{legacy}.update', {'id': item_id, 'fields': fields})


async def _add(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    fields = as_dict(args.get('fields'))
    if ctx.user_id and 'assignedById' not in fields:
        fields['assignedById'] = ctx.user_id
    res = await ctx.b24.call('crm.item.add', {'entityTypeId': et.id, 'fields': fields})
    item_id = int(res['item']['id'])
    await _set_comms(ctx, et, item_id, args)
    return {'ok': True, 'id': item_id, 'url': et.url(ctx.settings.portal_url, item_id)}


async def _update(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    fields = as_dict(args.get('fields'))
    if fields:
        await ctx.b24.call('crm.item.update', {'entityTypeId': et.id, 'id': item_id, 'fields': fields})
    await _set_comms(ctx, et, item_id, args)
    return {'ok': True, 'id': item_id, 'url': et.url(ctx.settings.portal_url, item_id)}


async def _comment_add(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    fields = {'ENTITY_ID': int(args['id']), 'ENTITY_TYPE': et.timeline_type, 'COMMENT': str(args['text'])}
    cid = await ctx.b24.call('crm.timeline.comment.add', {'fields': fields})
    return {'ok': True, 'comment_id': cid}


def register(tb: Toolbox) -> None:
    tb.add('crm_entity_types', 'Список типов CRM-объектов: стандартные (сделки, контакты, компании, лиды) и '
      'смарт-процессы с их entityTypeId. Вызывай, если не знаешь entityTypeId смарт-процесса.',
      schema({}), _entity_types)
    tb.add('crm_fields', 'Поля объекта CRM-типа: код, название, тип, обязательность, варианты списков. '
      'Нужно перед фильтрацией или изменением по пользовательским полям (ufCrm...).',
      schema({'entity': ENTITY, 'only_custom': {'type': 'boolean', 'description': 'только пользовательские поля'}},
              ['entity']), _fields)
    tb.add('crm_pipelines', 'Воронки (направления) и их стадии для сделок или смарт-процесса.',
      schema({'entity': ENTITY}, ['entity']), _pipelines)
    tb.add('crm_list', 'Список элементов CRM (crm.item.list) с фильтром. Фильтр — поля в camelCase с '
      'префиксами операций: {"stageId": "C2:NEW", "%title": "ромашка", ">opportunity": 1000, '
      '"assignedById": 5, ">=createdTime": "2026-01-01", "categoryId": 2}.',
      schema({
          'entity': ENTITY,
          'filter': {'type': 'object', 'description': 'фильтр crm.item.list'},
          'select': {'type': 'array', 'items': {'type': 'string'}, 'description': 'поля (по умолчанию основные)'},
          'order': {'type': 'object', 'description': 'сортировка, например {"id": "desc"}'},
          'limit': LIMIT,
      }, ['entity']), _list)
    tb.add('crm_search', 'Быстрый поиск объектов CRM по строке: название, ФИО, телефон или email.',
      schema({'entity': ENTITY, 'query': {'type': 'string'}, 'limit': LIMIT}, ['entity', 'query']), _search)
    tb.add('crm_get', 'Полная карточка элемента CRM с подписанными полями, стадией, ссылкой; '
      'для контактов/компаний/лидов — телефоны и email.',
      schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), _get)
    tb.add('crm_timeline', 'История элемента CRM: комментарии таймлайна и дела (звонки, письма, встречи).',
      schema({'entity': ENTITY, 'id': ITEM_ID, 'limit': LIMIT}, ['entity', 'id']), _timeline)
    tb.add('crm_related', 'Связанные с элементом объекты: контакты, компания, задачи.',
      schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), _related)
    tb.add('crm_add', 'Создать элемент CRM. fields — в camelCase ({"title": ..., "opportunity": 1000, '
      '"assignedById": 5, "categoryId": 2, "stageId": ..., "contactIds": [1], "ufCrm5_123": ...}). '
      'Для контакта/компании/лида телефон и email передай в phone/email.',
      schema({'entity': ENTITY, 'fields': {'type': 'object'}, 'phone': {'type': 'string'},
               'email': {'type': 'string'}}, ['entity', 'fields']), _add, write=True)
    tb.add('crm_update', 'Изменить поля элемента CRM (в том числе перевести на другую стадию: {"stageId": ...}).',
      schema({'entity': ENTITY, 'id': ITEM_ID, 'fields': {'type': 'object'}, 'phone': {'type': 'string'},
               'email': {'type': 'string'}}, ['entity', 'id', 'fields']), _update, write=True)
    tb.add('crm_comment_add', 'Написать комментарий в таймлайн элемента CRM (автор — пользователь вебхука, т.е. сервисная учётка агента).',
      schema({'entity': ENTITY, 'id': ITEM_ID, 'text': {'type': 'string'}}, ['entity', 'id', 'text']),
      _comment_add, write=True)
