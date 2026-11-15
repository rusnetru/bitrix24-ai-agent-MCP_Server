"""Инструменты агента: CRM (стандартные объекты и смарт-процессы), задачи, пользователи, таймлайн.

Один набор используется и встроенным LLM-агентом (чат-бот), и MCP-сервером.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.entities import EntityRegistry, EntityType
from b24agent.store import Store

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 15000

TASK_STATUS = {
    '1': 'новая', '2': 'ждёт выполнения', '3': 'выполняется', '4': 'ждёт контроля',
    '5': 'завершена', '6': 'отложена', '7': 'отклонена', '-1': 'просрочена',
}
TASK_SELECT = [
    'ID', 'TITLE', 'STATUS', 'REAL_STATUS', 'PRIORITY', 'RESPONSIBLE_ID', 'CREATED_BY',
    'DEADLINE', 'CREATED_DATE', 'CLOSED_DATE', 'GROUP_ID', 'UF_CRM_TASK',
]


@dataclass
class BotContext:
    """Контекст разговора в чате/таймлайне (для инструментов, работающих с ботом)."""

    imbot: Any  # b24agent.imbot.ImBot
    dialog_id: str | None = None
    conv: str = ''
    bound: tuple[int, int] | None = None  # (entityTypeId, id) объекта, к которому привязан разговор


@dataclass
class ToolContext:
    b24: Bitrix
    entities: EntityRegistry
    settings: Settings
    store: Store
    user_id: int | None = None
    bot: BotContext | None = None


Handler = Callable[[ToolContext, dict], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    write: bool = False
    bot_only: bool = False

    def openai_spec(self) -> dict:
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description, 'parameters': self.parameters,
        }}


def _schema(props: dict, required: list[str] | None = None) -> dict:
    return {'type': 'object', 'properties': props, 'required': required or []}


ENTITY = {'type': 'string', 'description': 'Тип объекта: deal, contact, company, lead, quote, invoice, '
          'entityTypeId смарт-процесса (например 1038) или его название'}
ITEM_ID = {'type': 'integer', 'description': 'ID элемента'}
LIMIT = {'type': 'integer', 'description': 'Сколько записей вернуть (по умолчанию 20, максимум 200)'}


def _i(args: dict, key: str, default: int | None = None) -> int | None:
    val = args.get(key, default)
    if val in (None, ''):
        return default
    return int(val)


def _obj(val: Any) -> dict:
    if isinstance(val, str):
        val = val.strip()
        return json.loads(val) if val else {}
    return dict(val or {})


def _empty(v: Any) -> bool:
    return v is None or v in ('', [], {}) or (v == '0' and False)


def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, '', [], {})}


class Toolbox:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self._fields_cache: dict[int, tuple[float, dict]] = {}
        self._stage_cache: dict[str, tuple[float, dict]] = {}
        self._user_cache: dict[int, str] = {}
        self._register_all()

    # ------------------------------------------------------------------ registry
    def add(self, name: str, description: str, params: dict, handler: Handler, *,
            write: bool = False, bot_only: bool = False) -> None:
        self.tools[name] = Tool(name, description, params, handler, write, bot_only)

    def available(self, settings: Settings, with_bot: bool) -> list[Tool]:
        return [
            t for t in self.tools.values()
            if (settings.allow_write or not t.write) and (with_bot or not t.bot_only)
        ]

    async def execute(self, ctx: ToolContext, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        if tool is None:
            return json.dumps({'error': f'Нет инструмента {name}'}, ensure_ascii=False)
        if tool.write and not ctx.settings.allow_write:
            return json.dumps({'error': 'Изменение данных отключено (AGENT_ALLOW_WRITE=false)'}, ensure_ascii=False)
        if tool.bot_only and ctx.bot is None:
            return json.dumps({'error': 'Инструмент доступен только из чата с ботом'}, ensure_ascii=False)
        try:
            result = await tool.handler(ctx, args or {})
        except BitrixError as exc:
            result = {'error': f'Битрикс24: {exc.code}', 'details': exc.description}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            result = {'error': str(exc)}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '… [обрезано, уточните фильтр или уменьшите limit]'
        return text

    # ------------------------------------------------------------------ helpers
    async def fields(self, ctx: ToolContext, et: EntityType) -> dict:
        cached = self._fields_cache.get(et.id)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        res = await ctx.b24.call('crm.item.fields', {'entityTypeId': et.id})
        fields = res.get('fields', res) if isinstance(res, dict) else {}
        self._fields_cache[et.id] = (time.monotonic(), fields)
        return fields

    @staticmethod
    def stage_entity_id(et: EntityType, category_id: int | None) -> str:
        cat = int(category_id or 0)
        if et.id == 1:
            return 'STATUS'
        if et.id == 7:
            return 'QUOTE_STATUS'
        if et.id == 2:
            return 'DEAL_STAGE' if cat == 0 else f'DEAL_STAGE_{cat}'
        return f'{et.code}_STAGE_{cat}'

    async def stages(self, ctx: ToolContext, et: EntityType, category_id: int | None) -> dict[str, dict]:
        key = self.stage_entity_id(et, category_id)
        cached = self._stage_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return cached[1]
        rows, _ = await ctx.b24.list('crm.status.list', {'filter': {'ENTITY_ID': key}, 'order': {'SORT': 'ASC'}},
                                     limit=200)
        data = {r['STATUS_ID']: {'name': r.get('NAME'), 'semantics': (r.get('EXTRA') or {}).get('SEMANTICS')
                                 or r.get('SEMANTICS')} for r in rows}
        self._stage_cache[key] = (time.monotonic(), data)
        return data

    async def user_name(self, ctx: ToolContext, user_id: int | None) -> str:
        if not user_id:
            return ''
        if user_id not in self._user_cache:
            try:
                users = await ctx.b24.call('user.get', {'ID': user_id})
                u = users[0] if users else {}
                self._user_cache[user_id] = ' '.join(filter(None, [u.get('NAME'), u.get('LAST_NAME')])) or f'#{user_id}'
            except BitrixError:
                self._user_cache[user_id] = f'#{user_id}'
        return self._user_cache[user_id]

    async def humanize(self, ctx: ToolContext, et: EntityType, item: dict) -> dict:
        """Подписать поля человеческими названиями, раскрыть списки и стадии."""
        meta = await self.fields(ctx, et)
        out: dict[str, Any] = {}
        for key, value in item.items():
            if value in (None, '', [], {}) or key in {'entityTypeId'}:
                continue
            m = meta.get(key) or {}
            title = m.get('title') or key
            items = m.get('items')
            if items and m.get('type') in {'enumeration', 'list'}:
                names = {str(i.get('ID')): i.get('VALUE') for i in items}
                value = [names.get(str(v), v) for v in value] if isinstance(value, list) else names.get(str(value), value)
            label = key if title == key else f'{title} [{key}]'
            out[label] = value
        stage = item.get('stageId')
        if stage and et.stages_enabled:
            st = (await self.stages(ctx, et, item.get('categoryId'))).get(stage)
            if st:
                out['Стадия (название)'] = st['name']
        if item.get('id'):
            out['url'] = et.url(ctx.settings.portal_url, int(item['id']))
        return out

    async def entity_summary(self, ctx: ToolContext, etid: int, item_id: int) -> str:
        """Короткое описание объекта для системного промпта."""
        et = await ctx.entities.get(etid)
        try:
            item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
        except (BitrixError, KeyError, TypeError):
            return f'{et.title} #{item_id} (не удалось загрузить)'
        human = await self.humanize(ctx, et, item)
        brief = json.dumps(human, ensure_ascii=False, default=str)
        if len(brief) > 4000:
            brief = brief[:4000] + '…'
        return f'{et.title} (entityTypeId={et.id}) #{item_id}: {brief}'

    # ------------------------------------------------------------------ tool impls
    def _register_all(self) -> None:
        a = self.add

        # ---- справочники
        a('crm_entity_types', 'Список типов CRM-объектов: стандартные (сделки, контакты, компании, лиды) и '
          'смарт-процессы с их entityTypeId. Вызывай, если не знаешь entityTypeId смарт-процесса.',
          _schema({}), self._entity_types)
        a('crm_fields', 'Поля объекта CRM-типа: код, название, тип, обязательность, варианты списков. '
          'Нужно перед фильтрацией или изменением по пользовательским полям (ufCrm...).',
          _schema({'entity': ENTITY, 'only_custom': {'type': 'boolean', 'description': 'только пользовательские поля'}},
                  ['entity']), self._fields)
        a('crm_pipelines', 'Воронки (направления) и их стадии для сделок или смарт-процесса.',
          _schema({'entity': ENTITY}, ['entity']), self._pipelines)

        # ---- чтение CRM
        a('crm_list', 'Список элементов CRM (crm.item.list) с фильтром. Фильтр — поля в camelCase с '
          'префиксами операций: {"stageId": "C2:NEW", "%title": "ромашка", ">opportunity": 1000, '
          '"assignedById": 5, ">=createdTime": "2026-01-01", "categoryId": 2}.',
          _schema({
              'entity': ENTITY,
              'filter': {'type': 'object', 'description': 'фильтр crm.item.list'},
              'select': {'type': 'array', 'items': {'type': 'string'}, 'description': 'поля (по умолчанию основные)'},
              'order': {'type': 'object', 'description': 'сортировка, например {"id": "desc"}'},
              'limit': LIMIT,
          }, ['entity']), self._list)
        a('crm_search', 'Быстрый поиск объектов CRM по строке: название, ФИО, телефон или email.',
          _schema({'entity': ENTITY, 'query': {'type': 'string'}, 'limit': LIMIT}, ['entity', 'query']), self._search)
        a('crm_get', 'Полная карточка элемента CRM с подписанными полями, стадией, ссылкой; '
          'для контактов/компаний/лидов — телефоны и email.',
          _schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), self._get)
        a('crm_timeline', 'История элемента CRM: комментарии таймлайна и дела (звонки, письма, встречи).',
          _schema({'entity': ENTITY, 'id': ITEM_ID, 'limit': LIMIT}, ['entity', 'id']), self._timeline)
        a('crm_related', 'Связанные с элементом объекты: контакты, компания, задачи.',
          _schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), self._related)

        # ---- изменение CRM
        a('crm_add', 'Создать элемент CRM. fields — в camelCase ({"title": ..., "opportunity": 1000, '
          '"assignedById": 5, "categoryId": 2, "stageId": ..., "contactIds": [1], "ufCrm5_123": ...}). '
          'Для контакта/компании/лида телефон и email передай в phone/email.',
          _schema({'entity': ENTITY, 'fields': {'type': 'object'}, 'phone': {'type': 'string'},
                   'email': {'type': 'string'}}, ['entity', 'fields']), self._add, write=True)
        a('crm_update', 'Изменить поля элемента CRM (в том числе перевести на другую стадию: {"stageId": ...}).',
          _schema({'entity': ENTITY, 'id': ITEM_ID, 'fields': {'type': 'object'}, 'phone': {'type': 'string'},
                   'email': {'type': 'string'}}, ['entity', 'id', 'fields']), self._update, write=True)
        a('crm_comment_add', 'Написать комментарий в таймлайн элемента CRM от имени ИИ-агента.',
          _schema({'entity': ENTITY, 'id': ITEM_ID, 'text': {'type': 'string'}}, ['entity', 'id', 'text']),
          self._comment_add, write=True)

        # ---- задачи
        a('task_list', 'Список задач. Фильтр — поля в верхнем регистре: {"RESPONSIBLE_ID": 5, "REAL_STATUS": [2,3], '
          '"%TITLE": "отчёт", "<DEADLINE": "2026-12-01", "GROUP_ID": 3}. Статусы: 2 ждёт, 3 выполняется, '
          '4 ждёт контроля, 5 завершена, 6 отложена. Задачи объекта CRM: entity + id.',
          _schema({'filter': {'type': 'object'}, 'entity': ENTITY, 'id': ITEM_ID, 'limit': LIMIT,
                   'order': {'type': 'object'}}), self._task_list)
        a('task_get', 'Подробности задачи: описание, участники, привязки к CRM, чек-лист.',
          _schema({'id': {'type': 'integer'}}, ['id']), self._task_get)
        a('task_comments', 'Комментарии к задаче.',
          _schema({'id': {'type': 'integer'}, 'limit': LIMIT}, ['id']), self._task_comments)
        a('task_add', 'Создать задачу. deadline — ISO 8601 (2026-11-20T18:00:00+03:00). '
          'Можно привязать к объекту CRM через entity + crm_id.',
          _schema({
              'title': {'type': 'string'}, 'description': {'type': 'string'},
              'responsible_id': {'type': 'integer', 'description': 'ID исполнителя (по умолчанию — автор запроса)'},
              'deadline': {'type': 'string'}, 'priority': {'type': 'integer', 'description': '2 — высокий, 1 — обычный'},
              'auditors': {'type': 'array', 'items': {'type': 'integer'}},
              'group_id': {'type': 'integer'}, 'entity': ENTITY, 'crm_id': ITEM_ID,
          }, ['title']), self._task_add, write=True)
        a('task_update', 'Изменить задачу. fields — поля в верхнем регистре (TITLE, DEADLINE, RESPONSIBLE_ID, '
          'DESCRIPTION, PRIORITY). action: complete | renew | start | defer — сменить статус.',
          _schema({'id': {'type': 'integer'}, 'fields': {'type': 'object'}, 'action': {'type': 'string'}}, ['id']),
          self._task_update, write=True)
        a('task_comment_add', 'Добавить комментарий к задаче.',
          _schema({'id': {'type': 'integer'}, 'text': {'type': 'string'}}, ['id', 'text']),
          self._task_comment_add, write=True)

        # ---- пользователи
        a('user_search', 'Найти сотрудников по ФИО, email или должности. Возвращает ID для назначения ответственных.',
          _schema({'query': {'type': 'string'}, 'limit': LIMIT}, ['query']), self._user_search)
        a('user_get', 'Данные сотрудника по ID (без id — текущий собеседник).',
          _schema({'id': {'type': 'integer'}}), self._user_get)

        # ---- работа бота с чатами и таймлайном
        a('chat_bind_entity', 'Привязать ТЕКУЩИЙ чат к элементу CRM/смарт-процесса: агент будет работать в '
          'контексте этого объекта. Без параметров — отвязать.',
          _schema({'entity': ENTITY, 'id': ITEM_ID}), self._chat_bind, bot_only=True)
        a('chat_create_for_entity', 'Создать отдельный групповой чат с ИИ-агентом по элементу CRM/смарт-процесса '
          '(участники: ответственный, автор запроса и user_ids) и оставить ссылку в таймлайне.',
          _schema({'entity': ENTITY, 'id': ITEM_ID, 'user_ids': {'type': 'array', 'items': {'type': 'integer'}}},
                  ['entity', 'id']), self._chat_create, write=True, bot_only=True)
        a('timeline_watch', 'Включить/выключить ответы агента на комментарии в таймлайне элемента CRM. '
          'После включения агент отвечает на комментарии этого объекта.',
          _schema({'entity': ENTITY, 'id': ITEM_ID, 'enabled': {'type': 'boolean'}}, ['entity', 'id']),
          self._timeline_watch, write=True, bot_only=True)

    # ---- справочники
    async def _entity_types(self, ctx: ToolContext, args: dict) -> Any:
        return [{'entityTypeId': e.id, 'code': e.code, 'title': e.title, 'smart_process': e.is_dynamic,
                 'has_pipelines': e.categories_enabled and e.stages_enabled}
                for e in await ctx.entities.all()]

    async def _fields(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        out = []
        for key, m in (await self.fields(ctx, et)).items():
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

    async def _pipelines(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        if not et.stages_enabled:
            return {'error': f'У типа «{et.title}» нет стадий'}
        cats = [{'id': 0, 'name': 'Основная'}]
        if et.categories_enabled:
            res = await ctx.b24.call('crm.category.list', {'entityTypeId': et.id})
            cats = [{'id': int(c['id']), 'name': c.get('name'), 'default': c.get('isDefault') in ('Y', True)}
                    for c in (res or {}).get('categories', [])] or cats
        for cat in cats:
            st = await self.stages(ctx, et, cat['id'])
            cat['stages'] = [{'stageId': k, **v} for k, v in st.items()]
        return {'entity': et.title, 'entityTypeId': et.id, 'pipelines': cats}

    # ---- чтение CRM
    @staticmethod
    def _default_select(et: EntityType) -> list[str]:
        if et.id == 3:
            return ['id', 'name', 'lastName', 'secondName', 'post', 'companyId', 'assignedById', 'createdTime']
        if et.id == 4:
            return ['id', 'title', 'companyType', 'industry', 'assignedById', 'createdTime']
        return ['id', 'title', 'stageId', 'categoryId', 'opportunity', 'currencyId', 'assignedById',
                'companyId', 'contactId', 'createdTime', 'updatedTime']

    async def _list(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        limit = min(_i(args, 'limit', 20) or 20, 200)
        params = {
            'entityTypeId': et.id,
            'filter': _obj(args.get('filter')),
            'select': args.get('select') or self._default_select(et),
            'order': _obj(args.get('order')) or {'id': 'desc'},
        }
        items, total = await ctx.b24.list('crm.item.list', params, limit=limit, key='items')
        stages = {}
        if et.stages_enabled:
            for cat in {i.get('categoryId') for i in items if i.get('stageId')}:
                stages.update(await self.stages(ctx, et, cat))
        rows = []
        for it in items:
            row = _clean(it)
            if it.get('stageId') in stages:
                row['stageName'] = stages[it['stageId']]['name']
            row['url'] = et.url(ctx.settings.portal_url, int(it['id']))
            rows.append(row)
        return {'entity': et.title, 'entityTypeId': et.id, 'total': total, 'count': len(rows), 'items': rows}

    async def _search(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        q = str(args['query']).strip()
        limit = min(_i(args, 'limit', 20) or 20, 100)
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
                        found.setdefault(int(r['ID']), _clean({
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
                'select': self._default_select(et), 'order': {'id': 'desc'}}, limit=limit, key='items')
            if not items and q.isdigit():
                items, _ = await ctx.b24.list('crm.item.list', {
                    'entityTypeId': et.id, 'filter': {'id': int(q)}, 'select': self._default_select(et)},
                    limit=1, key='items')
            for it in items:
                found[int(it['id'])] = _clean(it)
        for iid, row in found.items():
            row['url'] = et.url(ctx.settings.portal_url, iid)
        return {'entity': et.title, 'entityTypeId': et.id, 'items': list(found.values())[:limit]}

    async def _get(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        item_id = _i(args, 'id')
        item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
        out = {'entity': et.title, 'entityTypeId': et.id, **(await self.humanize(ctx, et, item))}
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
                out[f'{key}_name'] = await self.user_name(ctx, int(item[key]))
        return out

    async def _timeline(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        item_id = _i(args, 'id')
        limit = min(_i(args, 'limit', 20) or 20, 100)
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
            c['AUTHOR'] = await self.user_name(ctx, _int_or_none(c.get('AUTHOR_ID')))
        for act in acts:
            if act.get('DESCRIPTION'):
                act['DESCRIPTION'] = re.sub(r'<[^>]+>', ' ', act['DESCRIPTION'])[:500]
        return {'comments': comments, 'activities': [_clean(x) for x in acts]}

    async def _related(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        item_id = int(args['id'])
        item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
        out: dict[str, Any] = {}
        contact_ids = item.get('contactIds') or ([item['contactId']] if item.get('contactId') else [])
        if contact_ids:
            rows, _ = await ctx.b24.list('crm.contact.list', {
                'filter': {'ID': contact_ids}, 'select': ['ID', 'NAME', 'LAST_NAME', 'POST', 'PHONE', 'EMAIL']},
                limit=50)
            out['contacts'] = [_clean({
                'id': r['ID'], 'name': ' '.join(filter(None, [r.get('LAST_NAME'), r.get('NAME')])),
                'post': r.get('POST'), 'phone': [p['VALUE'] for p in r.get('PHONE') or []],
                'email': [p['VALUE'] for p in r.get('EMAIL') or []]}) for r in rows]
        if item.get('companyId'):
            comp = await ctx.b24.call('crm.company.get', {'id': item['companyId']})
            out['company'] = {'id': comp.get('ID'), 'title': comp.get('TITLE')}
        tasks, _ = await ctx.b24.list('tasks.task.list', {
            'filter': {'UF_CRM_TASK': f'{et.task_prefix}_{item_id}'}, 'select': TASK_SELECT}, limit=50, key='tasks')
        out['tasks'] = [self._task_row(t) for t in tasks]
        return out

    # ---- изменение CRM
    async def _set_comms(self, ctx: ToolContext, et: EntityType, item_id: int, args: dict) -> None:
        if et.id not in (1, 3, 4) or not (args.get('phone') or args.get('email')):
            return
        legacy = {1: 'lead', 3: 'contact', 4: 'company'}[et.id]
        fields = {}
        if args.get('phone'):
            fields['PHONE'] = [{'VALUE': args['phone'], 'VALUE_TYPE': 'WORK'}]
        if args.get('email'):
            fields['EMAIL'] = [{'VALUE': args['email'], 'VALUE_TYPE': 'WORK'}]
        await ctx.b24.call(f'crm.{legacy}.update', {'id': item_id, 'fields': fields})

    async def _add(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        fields = _obj(args.get('fields'))
        if ctx.user_id and 'assignedById' not in fields:
            fields['assignedById'] = ctx.user_id
        res = await ctx.b24.call('crm.item.add', {'entityTypeId': et.id, 'fields': fields})
        item_id = int(res['item']['id'])
        await self._set_comms(ctx, et, item_id, args)
        return {'ok': True, 'id': item_id, 'url': et.url(ctx.settings.portal_url, item_id)}

    async def _update(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        item_id = int(args['id'])
        fields = _obj(args.get('fields'))
        if fields:
            await ctx.b24.call('crm.item.update', {'entityTypeId': et.id, 'id': item_id, 'fields': fields})
        await self._set_comms(ctx, et, item_id, args)
        return {'ok': True, 'id': item_id, 'url': et.url(ctx.settings.portal_url, item_id)}

    async def _comment_add(self, ctx: ToolContext, args: dict) -> Any:
        et = await ctx.entities.get(args['entity'])
        fields = {'ENTITY_ID': int(args['id']), 'ENTITY_TYPE': et.timeline_type, 'COMMENT': str(args['text'])}
        bot_id = await ctx.store.get('bot_id')
        if bot_id:
            fields['AUTHOR_ID'] = bot_id
        cid = await ctx.b24.call('crm.timeline.comment.add', {'fields': fields})
        return {'ok': True, 'comment_id': cid}

    # ---- задачи
    @staticmethod
    def _task_row(t: dict) -> dict:
        status = str(t.get('subStatus') or t.get('realStatus') or t.get('status') or '')
        return _clean({
            'id': t.get('id'), 'title': t.get('title'), 'status': TASK_STATUS.get(status, status),
            'priority': t.get('priority'), 'responsibleId': t.get('responsibleId'),
            'responsible': (t.get('responsible') or {}).get('name'),
            'createdBy': t.get('createdBy'), 'deadline': t.get('deadline'), 'createdDate': t.get('createdDate'),
            'closedDate': t.get('closedDate'), 'groupId': t.get('groupId') if t.get('groupId') != '0' else None,
            'crm': t.get('ufCrmTask'),
        })

    async def _task_list(self, ctx: ToolContext, args: dict) -> Any:
        flt = _obj(args.get('filter'))
        if args.get('entity') and args.get('id'):
            et = await ctx.entities.get(args['entity'])
            flt['UF_CRM_TASK'] = f'{et.task_prefix}_{int(args["id"])}'
        limit = min(_i(args, 'limit', 20) or 20, 200)
        tasks, total = await ctx.b24.list('tasks.task.list', {
            'filter': flt, 'select': TASK_SELECT, 'order': _obj(args.get('order')) or {'ID': 'desc'}},
            limit=limit, key='tasks')
        portal = ctx.settings.portal_url
        rows = []
        for t in tasks:
            row = self._task_row(t)
            row['url'] = f'{portal}/company/personal/user/0/tasks/task/view/{t.get("id")}/'
            rows.append(row)
        return {'total': total, 'count': len(rows), 'tasks': rows}

    async def _task_get(self, ctx: ToolContext, args: dict) -> Any:
        res = await ctx.b24.call('tasks.task.get', {'taskId': int(args['id']), 'select': [
            *TASK_SELECT, 'DESCRIPTION', 'AUDITORS', 'ACCOMPLICES', 'TAGS', 'CHECKLIST', 'TIME_ESTIMATE']})
        t = res.get('task', res)
        row = self._task_row(t)
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
        return _clean(row)

    async def _task_comments(self, ctx: ToolContext, args: dict) -> Any:
        res = await ctx.b24.call('task.commentitem.getlist', {'TASKID': int(args['id']), 'ORDER': {'ID': 'desc'}})
        limit = min(_i(args, 'limit', 20) or 20, 100)
        return [{'id': c.get('ID'), 'author': c.get('AUTHOR_NAME'), 'date': c.get('POST_DATE'),
                 'text': re.sub(r'\s+', ' ', c.get('POST_MESSAGE') or '')[:2000]} for c in (res or [])[:limit]]

    async def _task_add(self, ctx: ToolContext, args: dict) -> Any:
        fields: dict[str, Any] = {'TITLE': args['title']}
        if args.get('description'):
            fields['DESCRIPTION'] = args['description']
        responsible = _i(args, 'responsible_id') or ctx.user_id
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

    async def _task_update(self, ctx: ToolContext, args: dict) -> Any:
        tid = int(args['id'])
        fields = _obj(args.get('fields'))
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

    async def _task_comment_add(self, ctx: ToolContext, args: dict) -> Any:
        fields = {'POST_MESSAGE': str(args['text'])}
        bot_id = await ctx.store.get('bot_id')
        if bot_id:
            fields['AUTHOR_ID'] = bot_id
        cid = await ctx.b24.call('task.commentitem.add', {'TASKID': int(args['id']), 'FIELDS': fields})
        return {'ok': True, 'comment_id': cid}

    # ---- пользователи
    @staticmethod
    def _user_row(u: dict) -> dict:
        return _clean({
            'id': int(u['ID']), 'name': ' '.join(filter(None, [u.get('LAST_NAME'), u.get('NAME'), u.get('SECOND_NAME')])),
            'position': u.get('WORK_POSITION'), 'email': u.get('EMAIL'), 'departments': u.get('UF_DEPARTMENT'),
            'active': u.get('ACTIVE'), 'phone': u.get('WORK_PHONE') or u.get('PERSONAL_MOBILE'),
        })

    async def _user_search(self, ctx: ToolContext, args: dict) -> Any:
        q = str(args['query']).strip()
        limit = min(_i(args, 'limit', 20) or 20, 100)
        rows, _ = await ctx.b24.list('user.search', {'FIND': q, 'ACTIVE': True}, limit=limit)
        if not rows:
            words = q.split()
            rows, _ = await ctx.b24.list('user.get', {'FILTER': {'%LAST_NAME': words[0], 'ACTIVE': True}}, limit=limit)
        return [self._user_row(u) for u in rows if not str(u.get('UF_USER_CRM_ENTITY') or '')]

    async def _user_get(self, ctx: ToolContext, args: dict) -> Any:
        uid = _i(args, 'id') or ctx.user_id
        if not uid:
            return {'error': 'Не указан id'}
        users = await ctx.b24.call('user.get', {'ID': uid})
        return self._user_row(users[0]) if users else {'error': 'не найден'}

    # ---- бот: чаты и таймлайн
    async def _chat_bind(self, ctx: ToolContext, args: dict) -> Any:
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

    async def _chat_create(self, ctx: ToolContext, args: dict) -> Any:
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
        chat_id = await ctx.bot.imbot.create_chat(
            title=f'ИИ: {et.title} «{title}»',
            users=sorted(users),
            entity_type='B24AGENT',
            entity_id=f'{et.id}|{item_id}',
            description=url,
            message=f'Чат по объекту [url={url}]{et.title} «{title}»[/url]. '
                    f'Пишите вопросы — я работаю в контексте этого объекта.',
        )
        dialog = f'chat{chat_id}'
        await ctx.store.bind_chat(dialog, et.id, item_id, ctx.user_id)
        chat_url = f'{ctx.settings.portal_url}/online/?IM_DIALOG={dialog}'
        bot_id = await ctx.store.get('bot_id')
        fields = {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type,
                  'COMMENT': f'Создан чат с ИИ-агентом: {chat_url}'}
        if bot_id:
            fields['AUTHOR_ID'] = bot_id
        try:
            await ctx.b24.call('crm.timeline.comment.add', {'fields': fields})
        except BitrixError as exc:
            log.warning('Не удалось оставить ссылку на чат в таймлайне: %s', exc)
        return {'ok': True, 'chat_id': chat_id, 'dialog_id': dialog, 'chat_url': chat_url}

    async def _timeline_watch(self, ctx: ToolContext, args: dict) -> Any:
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


def _int_or_none(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
