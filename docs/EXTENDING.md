# Как дорабатывать

Проект рассчитан на то, что его клонируют внутрь контура и развивают своими силами. Основная точка расширения — **инструменты**. Новый инструмент сразу доступен и встроенному агенту, и MCP-клиентам.

## Быстрый старт разработчика

```bash
uv sync --group dev
cp .env.example .env        # вебхук тестового портала, LiteLLM
uv run pytest               # тесты без портала и LLM (фейки)
uv run b24agent check       # связь с порталом и моделью
uv run b24agent tools       # какие инструменты подключены
uv run b24agent ask "найди сделки компании Ромашка"   # прогон агента из консоли
```

## Два способа добавить инструмент

### 1. Плагин (рекомендуется для доработок компании)

Положите файл в `plugins/` (каталог задаётся `PLUGINS_DIR`). Файлы, имя которых начинается с `_`, не загружаются. Можно также указать импортируемые модули в `TOOL_PLUGINS=company.tools.sales,company.tools.hr`.

```python
# plugins/hr.py
from b24agent.tools import Toolbox, ToolContext, schema, arg_int

def register(tb: Toolbox) -> None:
    @tb.tool('hr_vacations', 'Отпуска сотрудника на ближайший месяц. Вызывай на вопросы «кто в отпуске».',
             schema({'user_id': {'type': 'integer'}}))
    async def hr_vacations(ctx: ToolContext, args: dict):
        uid = arg_int(args, 'user_id') or ctx.user_id
        return await ctx.b24.call('calendar.accessibility.get', {'users': [uid], 'from': '...', 'to': '...'})
```

Полный пример с подсчётом воронки и изменяющим инструментом лежит в [`plugins/_example_sales.py`](../plugins/_example_sales.py).

Что ещё можно сделать в плагине:
- **переопределить встроенный инструмент**: зарегистрировать его под тем же именем, и в лог попадёт запись «переопределён»;
- **убрать встроенный**: `tb.remove('bp_start')`;
- **отключить без кода**: `TOOLS_DISABLED=bp_start,crm_add`.

### 2. Встроенный модуль

Это нужно, если доработка общая. Создайте `b24agent/tools/<area>.py` с функцией `register(tb)` и добавьте модуль в `BUILTIN_MODULES` в `b24agent/tools/__init__.py`.

## Контракт инструмента

```python
tb.add(name, description, parameters_json_schema, handler, write=False, bot_only=False)
# или декоратор @tb.tool(...)
async def handler(ctx: ToolContext, args: dict) -> dict | list | str
```

**Поля `ToolContext`:**

| Поле | Что это |
|---|---|
| `ctx.b24` | клиент REST: `await ctx.b24.call('method', {...})`; `await ctx.b24.list('method', params, limit=200, key='items')` (постраничная выборка); `await ctx.b24.raw(...)` (весь ответ, с `total`/`next`); `v3=True` (REST 3.0 на `/rest/api/`); `await ctx.b24.method_info('x.y')` (проверка, что метод есть на портале) |
| `ctx.entities` | справочник CRM-типов: `await ctx.entities.get('Проекты' \| 1038 \| 'deal')` → `EntityType` (`.id`, `.title`, `.timeline_type`, `.task_prefix`, `.url(portal, id)`) |
| `ctx.tb` | реестр и кэширующие помощники: `fields()`, `stages()`, `user_name()`, `humanize()`, `entity_summary()` |
| `ctx.settings` | настройки (`portal_url`, `allow_write`, …) |
| `ctx.store` | SQLite-хранилище: `get` / `set` для своих ключей |
| `ctx.user_id` | ID сотрудника, который спрашивает; `None` для MCP и консоли |
| `ctx.bot` | контекст чата (`dialog_id`, `bound`, `imbot`); только в разговоре с ботом |

**Правила:**
1. **`description` пишется для модели.** Укажите, когда вызывать инструмент, что он вернёт и формат параметров с примером. Это самый важный фактор качества. Для Qwen пишите по-русски и конкретно.
2. **Параметров должно быть мало, и они должны быть простыми.** Предпочитайте `entity` + `id` вместо сырых структур Битрикса. Числа объявляйте `integer`. Объекты могут прийти строкой JSON, поэтому используйте `as_dict()`.
3. **Возвращайте компактный JSON.** Убирайте пустые поля (`clean()`), добавляйте `url` на объект. Результат длиннее `MAX_RESULT_CHARS` (15000) обрезается.
4. **Ошибки.** `BitrixError` и `ValueError` / `KeyError` / `PermissionError` перехватываются, и модель получает `{"error": ...}`. Можно вернуть `{"error": "понятное объяснение"}` и самостоятельно.
5. **Помечайте изменяющие инструменты `write=True`.** Тогда они скрываются при `AGENT_ALLOW_WRITE=false`. Удаление лучше не делать вовсе или делать отдельным инструментом с явным подтверждением.
6. **`bot_only=True`**, если нужен чат (`ctx.bot`). Такие инструменты не публикуются в MCP.
7. **Методы, доступные только приложению** (`event.bind`, `placement.*`, `entity.*`, `bizproc.robot.*`, `crm.automation.trigger.execute`), через вебхук не работают и возвращают `WRONG_AUTH_TYPE`. Список приведён в [BITRIX_BOX_API.md](BITRIX_BOX_API.md).

**Тест на инструмент.** Пример с фейковым `FakeBitrix` и фейковой LLM — в `tests/test_agent.py`:

```python
async def test_my_tool(env, settings):
    b24, store, reg, tb = env
    ctx = ToolContext(b24=b24, entities=reg, settings=settings, store=store, tb=tb, user_id=3)
    res = json.loads(await tb.execute(ctx, 'hr_vacations', {}))
```

## Универсальный вызов вместо нового инструмента

Если нужно быстро, модель может вызвать любой метод через `bitrix_call`. Политика по умолчанию разрешает только чтение. Для внутренних сценариев можно открыть конкретные методы:

```env
BITRIX_CALL=full
BITRIX_CALL_ALLOW=crm.deal.productrows.set,catalog.product.add
BITRIX_CALL_DENY=crm.*.delete
```

Как только сценарий станет регулярным, оформите его отдельным инструментом. Модель гораздо надёжнее работает с узким инструментом, чем с «сырым» REST.

## Другие точки расширения

| Что | Где |
|---|---|
| Тон, регламенты, словарь компании (названия воронок, кодовые слова) | файл `AGENT_PROMPT_FILE`; базовый промпт — `BASE_PROMPT` в `agent.py` |
| Другая LLM или прокси | `llm.py`: OpenAI-совместимый `/chat/completions`; параметры модели — `LLM_EXTRA_BODY` |
| Режим вызова инструментов | `LLM_TOOL_MODE=auto\|native\|json`; JSON-протокол описан в `agent.py` (`JSON_PROTOCOL`) |
| Новые события бота (команды, реакции, клавиатуры) | `bot.py` (`handle`), `imbot.py` (`ImBotV2.send(keyboard=...)`) |
| Новый источник событий (например, своя очередь) | `BotService.submit({'event': ..., 'data': ...})` из любого цикла |
| Хранение (Postgres вместо SQLite) | `store.py`: интерфейс из простых асинхронных методов |
| HTTP-эндпоинты (например, вызов из БП через «Исходящий вебхук» робота) | `web.py` |
| MCP по сети (Streamable HTTP) | `mcp_server.py`: обёртка для `mcp.server.streamable_http` вместо stdio |
| Типы CRM, префиксы, ссылки | `entities.py` (`STANDARD`, `ALIASES`, `EntityType.url`) |

## Соглашения

- Python 3.11+, `async` везде, сторонних зависимостей минимум (`aiohttp`, `mcp`, `python-dotenv`).
- Линтер: `uv run ruff check b24agent tests`. Перед коммитом: `uv run pytest`.
- Строки и описания на русском: это видят модель и сотрудники.
- Новые переменные окружения добавляйте в `config.py`, `.env.example` и README.
- Работу с методами коробки проверяйте через `bitrix_method_info` или `method.get`: версии модулей в коробке отстают от облака.
