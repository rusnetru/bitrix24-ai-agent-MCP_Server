"""Пример подключения своего ИИ-агента к чату Bitrix24.

Запуск: ``python examples/custom_agent_example.py`` (после настройки ``.env``,
см. README.md и ``docs/source/ai_chat_agent.rst``).

Чтобы подключить СВОЕГО агента вместо примера, нужно:

1. Реализовать класс с методом ``async def handle_message(self, message) -> BotReply``
   (контракт :class:`src.domain.interfaces.agent_handler.AgentHandler`).
2. Внутри метода вызывать свою LLM, при необходимости — сервисы этого MCP-сервера
   (``ContactService``, ``DealService``) для подбора товаров/контактов.
3. Зарегистрировать экземпляр класса через ``chat_bot_service.register_agent(...)``.

Остальное (доступность бота в общих чатах, отправка ответов, комментарии в
таймлайн CRM) сервис берёт на себя.
"""

import asyncio

from src.application.services.chat_bot import ChatBotService
from src.config import SettingsManager
from src.domain.entities.bot_event import BotIncomingMessage, BotReply
from src.infrastructure.bot.webhook_server import run_webhook_server
from src.infrastructure.ioc import provider
from src.infrastructure.logging.logger import logger


class EchoSalesAgent:
    """Простейший пример агента: показывает, куда подключать свою LLM."""

    async def handle_message(self, message: BotIncomingMessage) -> BotReply:
        """Обработка сообщения менеджера.

        :param message: Входящее сообщение из чата Bitrix24.
        :return: Ответ, который увидит менеджер в том же чате.
        """
        # Замените эту строку вызовом своей LLM/агента, например:
        # answer = await my_llm_client.ask(message.text)  # noqa: ERA001
        answer = f"Получил запрос: «{message.text}». Здесь будет ответ ИИ-агента."
        return BotReply(text=answer)


async def _register(settings) -> tuple[ChatBotService, int]:  # noqa: ANN001
    """Регистрация бота в Bitrix24 и подключение агента.

    :param settings: Настройки приложения.
    :return: Сервис чат-бота и ID зарегистрированного бота.
    """
    bitrix_client = provider.provide_bitrix_client()
    imbot_client = provider.provide_imbot_client(bitrix_client)
    chat_bot_service = ChatBotService(imbot_client)

    bot_id = await chat_bot_service.register_bot(
        code=settings.BOT_CODE,
        event_handler_url=settings.BOT_EVENT_HANDLER_URL,
        name=settings.BOT_NAME,
    )
    if bot_id is None:
        msg = "Не удалось зарегистрировать чат-бота в Bitrix24"
        raise SystemExit(msg)

    chat_bot_service.register_agent(bot_id, EchoSalesAgent())
    return chat_bot_service, bot_id


def main() -> None:
    """Регистрация бота в Bitrix24, подключение агента и запуск приёма событий."""
    settings = SettingsManager.init()

    if not settings.BOT_EVENT_HANDLER_URL:
        msg = (
            "Не указана переменная окружения BOT_EVENT_HANDLER_URL — "
            "укажите публичный URL вида https://your-domain/bot/events"
        )
        raise SystemExit(msg)

    # Регистрация бота выполняется в отдельном event loop, который затем
    # закрывается — после этого aiohttp поднимает свой собственный loop.
    chat_bot_service, bot_id = asyncio.run(_register(settings))
    logger.info("Бот готов принимать сообщения", bot_id=bot_id)

    run_webhook_server(
        chat_bot_service,
        host=settings.BOT_HTTP_HOST,
        port=settings.BOT_HTTP_PORT,
    )


if __name__ == "__main__":
    main()
