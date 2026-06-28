"""Интерфейс агента, который можно подключить к чат-боту Bitrix24.

Это единственный контракт, который должен реализовать ваш собственный ИИ-агент,
чтобы подключиться к серверу: остальное (регистрация бота, приём вебхуков,
отправка сообщений и комментариев в таймлайн) берёт на себя
:class:`src.application.services.chat_bot.ChatBotService`.
"""

from typing import Protocol, runtime_checkable

from src.domain.entities.bot_event import BotIncomingMessage, BotReply


@runtime_checkable
class AgentHandler(Protocol):
    """Контракт обработчика сообщений для одного чат-бота Bitrix24.

    Пример::

        class MySalesAgent:
            async def handle_message(self, message: BotIncomingMessage) -> BotReply:
                answer = await call_my_llm(message.text)
                return BotReply(text=answer)

        chat_bot_service.register_agent("ai_sales_assistant", MySalesAgent())
    """

    async def handle_message(self, message: BotIncomingMessage) -> BotReply:
        """Обработка входящего сообщения и формирование ответа.

        :param message: Нормализованное входящее сообщение.
        :return: Ответ, который будет отправлен в чат (и опционально в таймлайн).
        """
        ...
