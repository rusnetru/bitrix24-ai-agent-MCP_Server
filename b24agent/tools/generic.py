"""Универсальный доступ к REST: любой метод коробки с политикой безопасности.

Нужен, чтобы агент (и разработчик через MCP) мог воспользоваться методом, для которого ещё нет
отдельного инструмента. Политика настраивается переменными окружения:

- ``BITRIX_CALL=off|read|full`` — выключено / только чтение (по умолчанию) / чтение и запись;
- ``BITRIX_CALL_DENY`` — дополнительные запрещённые шаблоны (fnmatch, через запятую);
- ``BITRIX_CALL_ALLOW`` — явно разрешённые шаблоны (перекрывают запреты, кроме ``batch``).

Метод считается «читающим», если оканчивается на .get/.list/.fields/... (см. READ_SUFFIXES).
Запись дополнительно требует ``AGENT_ALLOW_WRITE=true``.
"""

from __future__ import annotations

from fnmatch import fnmatch
from typing import Any

from b24agent.tools.base import Toolbox, ToolContext, as_dict, schema

READ_SUFFIXES = (
    '.get', '.list', '.fields', '.search', '.getlist', '.getfields', '.field.list', '.field.get', '.getchildren',
    '.count', '.aggregate', '.current', '.tail', '.getbyid', '.nearest', '.accessibility.get', '.types',
    '.getmanifest', '.entity.types', '.findbycomm', '.gettypes', '.getversions', '.children', '.subordinates',
    '.getaccess', '.access.get', '.counters.get', '.history.list', '.status', '.settings', '.settings.get',
)
READ_EXACT = {'profile', 'scope', 'method.get', 'server.time', 'methods', 'user.access', 'access.name',
              'feature.get', 'app.info', 'user.admin', 'rest.documentation.openapi', 'rest.scope.list',
              'imbot.v2.revision.get', 'crm.settings.mode.get', 'crm.enum.ownertype'}
# Даже в режиме full эти методы закрыты, если не разрешены явно через BITRIX_CALL_ALLOW
DEFAULT_DENY = (
    '*.delete', '*.deletetree', '*.markdeleted', '*.unregister', '*.kill', '*.terminate', 'event.*',
    'placement.*', 'user.add', 'user.update', '*.userfield.*', 'crm.type.*', 'app.option.*', 'user.option.*',
    'imbot.*', 'imconnector.*', 'ai.engine.*', 'bizproc.workflow.template.*', 'bizproc.robot.*',
    'bizproc.activity.*', '*.getexternallink', 'disk.*.sharetouser', '*.rights.*', 'humanresources.node.member.*',
    'timeman.*.set', 'crm.entity.mergebatch', 'lists.delete', 'sonet_group.setowner',
)


def is_read_method(method: str) -> bool:
    m = method.lower()
    return m in READ_EXACT or m.endswith(READ_SUFFIXES)


def check_policy(ctx: ToolContext, method: str) -> str | None:
    """Вернёт текст ошибки, если метод вызывать нельзя."""
    s = ctx.settings
    m = method.lower().strip()
    if s.bitrix_call == 'off':
        return 'Универсальный вызов отключён (BITRIX_CALL=off)'
    if m in {'batch', 'batch.v3'} or m.endswith('.batch'):
        return 'batch через универсальный вызов запрещён — вызывайте методы по одному'
    allowed = any(fnmatch(m, p) for p in s.bitrix_call_allow)
    if is_read_method(m):
        return None if allowed or not any(fnmatch(m, p) for p in s.bitrix_call_deny) else f'Метод {method} запрещён'
    if not allowed:
        if s.bitrix_call != 'full':
            return f'{method} изменяет данные; универсальный вызов разрешён только для чтения (BITRIX_CALL=read)'
        if any(fnmatch(m, p) for p in (*DEFAULT_DENY, *s.bitrix_call_deny)):
            return f'Метод {method} запрещён политикой (добавьте в BITRIX_CALL_ALLOW, если он нужен)'
    if not s.allow_write:
        return 'Изменение данных отключено (AGENT_ALLOW_WRITE=false)'
    return None


async def _bitrix_call(ctx: ToolContext, args: dict) -> Any:
    method = str(args.get('method') or '').strip()
    if not method:
        return {'error': 'Укажите method'}
    err = check_policy(ctx, method)
    if err:
        return {'error': err}
    params = as_dict(args.get('params'))
    v3 = str(args.get('api_version') or '2') == '3'
    data = await ctx.b24.raw(method, params, v3=v3)
    return {k: data[k] for k in ('result', 'total', 'next') if k in data}


async def _method_info(ctx: ToolContext, args: dict) -> Any:
    names = args.get('methods') or [args.get('method')]
    out = {}
    for name in names:
        if name:
            info = await ctx.b24.method_info(str(name))
            info['policy'] = check_policy(ctx, str(name)) or 'allowed'
            out[str(name)] = info
    return out


def register(tb: Toolbox) -> None:
    tb.add('bitrix_call', 'Вызвать произвольный метод REST API Битрикс24, если для задачи нет отдельного '
           'инструмента. Параметры — как в документации метода (apidocs.bitrix24.ru). По умолчанию разрешены '
           'только читающие методы (*.get, *.list, *.fields ...). api_version=3 — REST 3.0 (/rest/api/).',
           schema({'method': {'type': 'string', 'description': 'например calendar.event.get'},
                   'params': {'type': 'object', 'description': 'параметры метода'},
                   'api_version': {'type': 'string', 'enum': ['2', '3']}}, ['method']), _bitrix_call)
    tb.add('bitrix_method_info', 'Проверить, есть ли метод на этом портале и доступен ли он по правам вебхука '
           '(method.get), и разрешён ли он политикой агента.',
           schema({'methods': {'type': 'array', 'items': {'type': 'string'}}, 'method': {'type': 'string'}}),
           _method_info)
