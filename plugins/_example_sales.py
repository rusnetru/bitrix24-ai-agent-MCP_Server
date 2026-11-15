"""Пример плагина компании. Скопируйте в plugins/<имя>.py (без «_» в начале — такие файлы не загружаются).

Плагин — обычный Python-модуль с функцией ``register(tb)``. В ней можно:
- добавить инструмент (``@tb.tool`` или ``tb.add``);
- переопределить встроенный (зарегистрировать под тем же именем);
- убрать встроенный (``tb.remove('crm_todo_add')``).

Инструмент сразу появляется и у встроенного агента (чат-бот), и в MCP (``b24agent mcp``).
Проверить: ``b24agent tools`` и ``b24agent ask "покажи воронку продаж за неделю"``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from b24agent.tools import Toolbox, ToolContext, arg_int, schema


def register(tb: Toolbox) -> None:
    @tb.tool(
        'sales_funnel_week',
        # Описание читает модель: пишите, КОГДА вызывать инструмент и что он вернёт.
        'Сводка по сделкам, созданным за последние N дней: количество и сумма по стадиям. '
        'Вызывай, когда спрашивают про воронку, динамику продаж или «что нового в сделках».',
        schema({
            'days': {'type': 'integer', 'description': 'за сколько дней (по умолчанию 7)'},
            'category_id': {'type': 'integer', 'description': 'ID воронки (по умолчанию все)'},
        }),
    )
    async def sales_funnel_week(ctx: ToolContext, args: dict) -> Any:
        days = arg_int(args, 'days', 7) or 7
        since = (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec='seconds')
        flt: dict = {'>=createdTime': since}
        if args.get('category_id') is not None:
            flt['categoryId'] = int(args['category_id'])
        items, _ = await ctx.b24.list('crm.item.list', {
            'entityTypeId': 2, 'filter': flt, 'select': ['id', 'stageId', 'categoryId', 'opportunity']},
            limit=5000, key='items')
        deal = await ctx.entities.get('deal')
        summary: dict[str, dict] = {}
        for it in items:
            stages = await ctx.tb.stages(ctx, deal, it.get('categoryId'))  # кэшируется
            name = (stages.get(it['stageId']) or {}).get('name', it['stageId'])
            row = summary.setdefault(name, {'count': 0, 'sum': 0.0})
            row['count'] += 1
            row['sum'] += float(it.get('opportunity') or 0)
        return {'period_days': days, 'total': len(items), 'by_stage': summary}

    # Пример записи с подтверждением через флаг write=True:
    # инструмент скрыт при AGENT_ALLOW_WRITE=false и помечен как изменяющий данные.
    @tb.tool('sales_mark_hot', 'Пометить сделку как «горячую» (пользовательское поле UF_CRM_HOT).',
             schema({'id': {'type': 'integer'}}, ['id']), write=True)
    async def sales_mark_hot(ctx: ToolContext, args: dict) -> Any:
        await ctx.b24.call('crm.item.update', {'entityTypeId': 2, 'id': int(args['id']),
                                               'fields': {'ufCrmHot': 'Y'}})
        return {'ok': True}
