"""Ядро системы инструментов: описание инструмента, контекст вызова, реестр (Toolbox), общие помощники.

Инструмент = имя + описание для модели + JSON Schema параметров + асинхронный обработчик
``async def handler(ctx: ToolContext, args: dict) -> Any``. Результат (dict/list/str) сериализуется в JSON
и отдаётся модели (встроенному агенту) или MCP-клиенту.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.entities import EntityRegistry, EntityType
from b24agent.store import Store

log = logging.getLogger(__name__)

MAX_RESULT_CHARS = 15000

TASK_STATUS = {
    '1': 'новая', '2': 'ждёт выполнения', '3': 'выполняется', '4': 'ждёт контроля',
    '5': 'завершена', '6': 'отложена', '7': 'отклонена', '-1': 'просрочена', '-2': 'не просмотрена',
    '-3': 'почти просрочена',
}
TASK_SELECT = [
    'ID', 'TITLE', 'STATUS', 'REAL_STATUS', 'PRIORITY', 'RESPONSIBLE_ID', 'CREATED_BY',
    'DEADLINE', 'CREATED_DATE', 'CLOSED_DATE', 'GROUP_ID', 'UF_CRM_TASK',
]

# Повторно используемые фрагменты JSON Schema
ENTITY = {'type': 'string', 'description': 'Тип объекта: deal, contact, company, lead, quote, invoice, '
          'entityTypeId смарт-процесса (например 1038) или его название'}
ITEM_ID = {'type': 'integer', 'description': 'ID элемента'}
LIMIT = {'type': 'integer', 'description': 'Сколько записей вернуть (по умолчанию 20, максимум 200)'}


@dataclass
class BotContext:
    """Контекст разговора в чате/таймлайне (для инструментов, работающих с ботом)."""

    imbot: Any  # b24agent.imbot.ImBotV1 | ImBotV2
    dialog_id: str | None = None
    conv: str = ''
    bound: tuple[int, int] | None = None  # (entityTypeId, id) объекта, к которому привязан разговор


@dataclass
class ToolContext:
    """Всё, что нужно обработчику: клиент REST, справочник типов, настройки, хранилище, кто спрашивает."""

    b24: Bitrix
    entities: EntityRegistry
    settings: Settings
    store: Store
    tb: Toolbox = None  # type: ignore[assignment]  # заполняется Toolbox.execute
    user_id: int | None = None  # ID сотрудника, от имени которого идёт запрос (None — MCP/консоль)
    bot: BotContext | None = None


Handler = Callable[[ToolContext, dict], Awaitable[Any]]


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Handler
    write: bool = False  # меняет данные — скрывается при AGENT_ALLOW_WRITE=false
    bot_only: bool = False  # нужен контекст разговора с ботом — не публикуется в MCP
    group: str = ''  # модуль-источник (для документации и отладки)
    meta: dict = field(default_factory=dict)

    def openai_spec(self) -> dict:
        return {'type': 'function', 'function': {
            'name': self.name, 'description': self.description, 'parameters': self.parameters,
        }}


# ---------------------------------------------------------------- помощники для обработчиков
def schema(props: dict, required: list[str] | None = None) -> dict:
    """Короткая запись JSON Schema объекта параметров."""
    return {'type': 'object', 'properties': props, 'required': required or []}


def arg_int(args: dict, key: str, default: int | None = None) -> int | None:
    val = args.get(key, default)
    if val in (None, ''):
        return default
    return int(val)


def as_dict(val: Any) -> dict:
    """Модели иногда передают объект строкой JSON — принимаем оба варианта."""
    if isinstance(val, str):
        val = val.strip()
        return json.loads(val) if val else {}
    return dict(val or {})


def clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, '', [], {})}


def to_int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def task_row(t: dict) -> dict:
    status = str(t.get('subStatus') or t.get('realStatus') or t.get('status') or '')
    return clean({
        'id': t.get('id'), 'title': t.get('title'), 'status': TASK_STATUS.get(status, status),
        'priority': t.get('priority'), 'responsibleId': t.get('responsibleId'),
        'responsible': (t.get('responsible') or {}).get('name'),
        'createdBy': t.get('createdBy'), 'deadline': t.get('deadline'), 'createdDate': t.get('createdDate'),
        'closedDate': t.get('closedDate'), 'groupId': t.get('groupId') if t.get('groupId') != '0' else None,
        'crm': t.get('ufCrmTask'),
    })


# ---------------------------------------------------------------- реестр
class Toolbox:
    def __init__(self) -> None:
        self.tools: dict[str, Tool] = {}
        self._fields_cache: dict[int, tuple[float, dict]] = {}
        self._stage_cache: dict[str, tuple[float, dict]] = {}
        self._user_cache: dict[int, str] = {}
        self._group = ''

    # --- регистрация
    def add(self, name: str, description: str, params: dict, handler: Handler, *,
            write: bool = False, bot_only: bool = False, **meta: Any) -> None:
        if name in self.tools:
            log.info('Инструмент %s переопределён (%s → %s)', name, self.tools[name].group, self._group)
        self.tools[name] = Tool(name, description, params, handler, write, bot_only, self._group, meta)

    def tool(self, name: str, description: str, params: dict | None = None, *,
             write: bool = False, bot_only: bool = False, **meta: Any) -> Callable[[Handler], Handler]:
        """Декоратор для плагинов::

            @tb.tool('my_report', 'Описание для модели', schema({'days': {'type': 'integer'}}))
            async def my_report(ctx, args): ...
        """
        def deco(fn: Handler) -> Handler:
            self.add(name, description, params or schema({}), fn, write=write, bot_only=bot_only, **meta)
            return fn
        return deco

    def remove(self, name: str) -> None:
        self.tools.pop(name, None)

    def available(self, settings: Settings, with_bot: bool) -> list[Tool]:
        return [
            t for t in self.tools.values()
            if (settings.allow_write or not t.write) and (with_bot or not t.bot_only)
            and t.name not in settings.tools_disabled
        ]

    async def execute(self, ctx: ToolContext, name: str, args: dict) -> str:
        tool = self.tools.get(name)
        ctx.tb = self
        if tool is None or name in ctx.settings.tools_disabled:
            return json.dumps({'error': f'Нет инструмента {name}'}, ensure_ascii=False)
        if tool.write and not ctx.settings.allow_write:
            return json.dumps({'error': 'Изменение данных отключено (AGENT_ALLOW_WRITE=false)'}, ensure_ascii=False)
        if tool.bot_only and ctx.bot is None:
            return json.dumps({'error': 'Инструмент доступен только из чата с ботом'}, ensure_ascii=False)
        try:
            result = await tool.handler(ctx, args or {})
        except BitrixError as exc:
            result = {'error': f'Битрикс24: {exc.code}', 'details': exc.description}
        except (ValueError, TypeError, KeyError, json.JSONDecodeError, PermissionError) as exc:
            result = {'error': str(exc)}
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + '… [обрезано, уточните фильтр или уменьшите limit]'
        return text

    # --- общие помощники с кэшем (используются разными модулями)
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
        """ENTITY_ID для crm.status.list: DEAL_STAGE[_N], STATUS, QUOTE_STATUS, DYNAMIC_<id>_STAGE_<cat> ..."""
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
