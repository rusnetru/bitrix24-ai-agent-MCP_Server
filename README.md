# Bitrix24 MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/MCP-compatible-purple.svg)](https://modelcontextprotocol.io/)

**MCP-сервер для Bitrix24 CRM: подключите AI-агента к сделкам, контактам и чатам за 5 минут.**

Bitrix24 MCP Server предоставляет стандартизированный [Model Context Protocol](https://modelcontextprotocol.io/) интерфейс, через который любой AI-агент (Claude, GPT, DeepSeek, локальные LLM) может:

- 🔍 Искать контакты, сделки, лиды
- 📝 Создавать и обновлять CRM-сущности
- 💬 Отвечать в чатах Bitrix24 как обычный сотрудник
- 📋 Писать комментарии в таймлайн сделок

Всё через единый протокол. Без написания REST-обёрток. Без парсинга вебхуков. Один раз настроил — и забыл.

## Быстрый старт (5 минут)

### 1. Установка

```bash
pip install bitrix24-mcp
```

### 2. Настройка

Создайте входящий вебхук в Bitrix24 (права: `crm`, `imbot`, `user`) и пропишите URL:

```bash
export BITRIX_WEBHOOK_URL="https://your.bitrix24.ru/rest/1/your-token/"
```

### 3. Запуск

```bash
bitrix24-mcp
```

Готово. Сервер слушает stdio — можно подключать AI-клиент (Claude Desktop, Cursor, Hermes Agent).

## Подключение AI-агента к чатам Bitrix24

Сервер включает готовый слой для чат-ботов (`imbot`):

```python
from src.domain.entities.bot_event import BotIncomingMessage, BotReply
from src.application.services.chat_bot import ChatBotService

class MyAIAgent:
    """Ваш AI-агент. Единственное, что нужно написать."""

    async def handle_message(self, message: BotIncomingMessage) -> BotReply:
        # Вызовите свою LLM
        answer = await my_llm.ask(message.text)
        return BotReply(
            text=answer,
            # Опционально: дублировать в таймлайн сделки
            timeline_comment=TimelineCommentTarget(
                entity_type="deal",
                entity_id=123
            )
        )

# Регистрация — остальное сервер берёт на себя
chat_bot_service.register_agent(bot_id, MyAIAgent())
```

После регистрации ваш AI-агент появляется в общих чатах Bitrix24 как обычный сотрудник. Менеджер пишет — агент отвечает. Комментарии автоматически попадают в таймлайн.

## MCP-инструменты

### CRM: Контакты

| Инструмент | Описание |
|-----------|----------|
| `get_contact` | Получить контакт по ID |
| `search_contacts` | Поиск по имени, телефону, email |
| `list_contacts` | Список с фильтрацией |

### CRM: Сделки

| Инструмент | Описание |
|-----------|----------|
| `get_deal` | Сделка по ID |
| `list_deals` | Список с фильтрами (активные, по контакту) |
| `update_deal_stage` | Передвинуть сделку по воронке |
| `create_deal` | Создать новую сделку |

### CRM: Лиды

| Инструмент | Описание |
|-----------|----------|
| `create_lead` | Создать лид |
| `list_leads` | Список с поиском |
| `get_lead` | Лид по ID |

### Чат-бот

| Инструмент | Описание |
|-----------|----------|
| `ChatBotService.register_bot` | Зарегистрировать бота в портале |
| `ChatBotService.register_agent` | Подключить AI-агента к боту |
| `ChatBotService.dispatch_event` | Обработка входящих сообщений |

### Задачи

| Инструмент | Описание |
|-----------|----------|
| `search_tasks` | Поиск задач |
| `create_task` | Создать задачу |
| `get_task_by_id` | Задача по ID |

## MCP-ресурсы

AI-агент может запрашивать данные по URI:

| Ресурс | Пример | Назначение |
|--------|--------|------------|
| `contact://{id}` | `contact://123` | Данные контакта |
| `deal://{id}` | `deal://456` | Данные сделки |
| `deals://active` | `deals://active` | Все активные сделки |

## Подключение к AI-клиентам

### Claude Desktop

```json
{
  "mcpServers": {
    "bitrix24": {
      "command": "bitrix24-mcp",
      "env": {
        "BITRIX_WEBHOOK_URL": "https://your.bitrix24.ru/rest/1/token/"
      }
    }
  }
}
```

### Hermes Agent

```yaml
mcp_servers:
  bitrix24:
    transport: stdio
    command: bitrix24-mcp
    env:
      BITRIX_WEBHOOK_URL: "https://your.bitrix24.ru/rest/1/token/"
```

### Cursor / VS Code

Добавьте в `.cursor/mcp.json` или настройки Cursor MCP.

## Чат-бот: полный пример

```bash
export BOT_EVENT_HANDLER_URL="https://your-domain.example.com/bot/events"
python examples/custom_agent_example.py
```

Сервер регистрирует бота, поднимает HTTP-обработчик вебхуков и подключает AI-агента. Подробнее: [docs/source/ai_chat_agent.rst](docs/source/ai_chat_agent.rst).

## Архитектура

```
AI-клиент (Claude/DeepSeek/GPT)
        │
        ▼ MCP (stdio)
┌───────────────────────┐
│  Bitrix24 MCP Server  │
│                       │
│  ┌─────────────────┐  │
│  │  MCP Handlers   │  │  ← CRM: contacts, deals, leads
│  ├─────────────────┤  │
│  │  ChatBotService │  │  ← imbot: register, dispatch, reply
│  ├─────────────────┤  │
│  │  WebhookServer  │  │  ← HTTP: приём событий Bitrix24
│  └─────────────────┘  │
└───────────┬───────────┘
            │ REST API (fast_bitrix24)
            ▼
     Bitrix24 Cloud
```

## Требования

- Python 3.12+
- Входящий вебхук Bitrix24 (права: `crm`, `imbot`, `user`)
- Для чат-бота: публичный HTTPS URL (ngrok, cloudflared, или любой хостинг) для приёма вебхуков

## Лицензия

MIT. Подробности в [LICENSE](LICENSE).

## Ссылки

- [Model Context Protocol](https://modelcontextprotocol.io/)
- [Bitrix24 REST API](https://dev.1c-bitrix.ru/rest_help/)
- [AgentIREST — AI-агенты для бизнеса](https://agentirest.com)
