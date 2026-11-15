"""Оргструктура и коллаборация: подразделения, рабочие группы/проекты, календарь."""

from __future__ import annotations

from typing import Any

from b24agent.tools.base import (
    LIMIT,
    Toolbox,
    ToolContext,
    arg_int,
    as_dict,
    clean,
    schema,
)


async def _departments(ctx: ToolContext, args: dict) -> Any:
    params: dict = {}
    if args.get('id'):
        params['ID'] = int(args['id'])
    if args.get('parent_id') is not None:
        params['PARENT'] = int(args['parent_id'])
    if args.get('name'):
        params['NAME'] = args['name']
    rows, total = await ctx.b24.list('department.get', params, limit=min(arg_int(args, 'limit', 100) or 100, 500))
    out = []
    for d in rows:
        row = clean({'id': d.get('ID'), 'name': d.get('NAME'), 'parent': d.get('PARENT'), 'head': d.get('UF_HEAD')})
        if row.get('head'):
            row['head_name'] = await ctx.tb.user_name(ctx, int(row['head']))
        out.append(row)
    if args.get('with_users') and args.get('id'):
        users, _ = await ctx.b24.list('user.get', {'FILTER': {'UF_DEPARTMENT': int(args['id']), 'ACTIVE': True}},
                                      limit=200)
        return {'departments': out, 'users': [clean({'id': u['ID'], 'name': f'{u.get("LAST_NAME", "")} '
                                                     f'{u.get("NAME", "")}'.strip(),
                                                     'position': u.get('WORK_POSITION')}) for u in users]}
    return {'total': total, 'departments': out}


async def _workgroups(ctx: ToolContext, args: dict) -> Any:
    flt = as_dict(args.get('filter'))
    if args.get('query'):
        flt['%NAME'] = args['query']
    res = await ctx.b24.raw('socialnetwork.api.workgroup.list', {
        'filter': flt, 'select': ['ID', 'NAME', 'DESCRIPTION', 'PROJECT', 'OWNER_ID', 'NUMBER_OF_MEMBERS',
                                  'DATE_ACTIVITY', 'CLOSED', 'OPENED'],
        'order': {'DATE_ACTIVITY': 'DESC'}})
    groups = (res.get('result') or {}).get('workgroups', [])
    limit = min(arg_int(args, 'limit', 20) or 20, 50)
    portal = ctx.settings.portal_url
    return [{**clean(g), 'url': f'{portal}/workgroups/group/{g.get("id")}/'} for g in groups[:limit]]


async def _calendar_events(ctx: ToolContext, args: dict) -> Any:
    owner_type = args.get('type') or 'user'
    owner = arg_int(args, 'owner_id') or (ctx.user_id if owner_type == 'user' else 0)
    params: dict = {'type': owner_type, 'ownerId': owner or 0}
    if args.get('from'):
        params['from'] = args['from']
    if args.get('to'):
        params['to'] = args['to']
    events = await ctx.b24.call('calendar.event.get', params) or []
    keep = ('ID', 'NAME', 'DATE_FROM', 'DATE_TO', 'TZ_FROM', 'DT_SKIP_TIME', 'LOCATION', 'DESCRIPTION',
            'MEETING_STATUS', 'MEETING_HOST', 'ATTENDEES_CODES', 'RRULE', 'UF_CRM_CAL_EVENT')
    limit = min(arg_int(args, 'limit', 50) or 50, 200)
    return [clean({k: e.get(k) for k in keep}) for e in events[:limit]]


async def _calendar_accessibility(ctx: ToolContext, args: dict) -> Any:
    users = [int(u) for u in args.get('user_ids') or []]
    return await ctx.b24.call('calendar.accessibility.get', {'users': users, 'from': args['from'], 'to': args['to']})


async def _calendar_event_add(ctx: ToolContext, args: dict) -> Any:
    owner = arg_int(args, 'owner_id') or ctx.user_id
    if not owner:
        return {'error': 'Укажите owner_id (чей календарь)'}
    params: dict = {
        'type': 'user', 'ownerId': owner, 'name': args['name'], 'from': args['from'], 'to': args['to'],
        'description': args.get('description', ''), 'section': arg_int(args, 'section_id') or None,
    }
    if args.get('attendees'):
        params.update({'is_meeting': 'Y', 'attendees': [int(x) for x in args['attendees']], 'host': owner})
    if args.get('location'):
        params['location'] = args['location']
    if not params['section']:
        sections = await ctx.b24.call('calendar.section.get', {'type': 'user', 'ownerId': owner}) or []
        if not sections:
            return {'error': 'У пользователя нет календаря'}
        params['section'] = int(sections[0]['ID'])
    eid = await ctx.b24.call('calendar.event.add', params)
    return {'ok': True, 'id': eid}


def register(tb: Toolbox) -> None:
    tb.add('department_list', 'Подразделения компании (department.get): по id, родителю или названию. '
           'with_users=true вместе с id — сотрудники подразделения.',
           schema({'id': {'type': 'integer'}, 'parent_id': {'type': 'integer'}, 'name': {'type': 'string'},
                   'with_users': {'type': 'boolean'}, 'limit': LIMIT}), _departments)
    tb.add('workgroup_list', 'Рабочие группы и проекты (socialnetwork.api.workgroup.list). query — часть названия.',
           schema({'query': {'type': 'string'}, 'filter': {'type': 'object'}, 'limit': LIMIT}), _workgroups)
    tb.add('calendar_events', 'События календаря. type: user (по умолчанию — календарь собеседника) | group | '
           'company_calendar; from/to — даты YYYY-MM-DD.',
           schema({'type': {'type': 'string'}, 'owner_id': {'type': 'integer'}, 'from': {'type': 'string'},
                   'to': {'type': 'string'}, 'limit': LIMIT}), _calendar_events)
    tb.add('calendar_accessibility', 'Занятость сотрудников в интервале (calendar.accessibility.get) — '
           'чтобы подобрать время встречи.',
           schema({'user_ids': {'type': 'array', 'items': {'type': 'integer'}}, 'from': {'type': 'string'},
                   'to': {'type': 'string'}}, ['user_ids', 'from', 'to']), _calendar_accessibility)
    tb.add('calendar_event_add', 'Создать событие/встречу в календаре сотрудника. from/to — '
           '"2026-11-20 15:00:00" или ISO 8601; attendees — ID участников.',
           schema({'name': {'type': 'string'}, 'from': {'type': 'string'}, 'to': {'type': 'string'},
                   'owner_id': {'type': 'integer'}, 'description': {'type': 'string'},
                   'attendees': {'type': 'array', 'items': {'type': 'integer'}}, 'location': {'type': 'string'},
                   'section_id': {'type': 'integer'}}, ['name', 'from', 'to']), _calendar_event_add, write=True)
