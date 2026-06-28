"""Сервис подключения внешних ИИ-агентов к чатам и таймлайну Bitrix24.

Это единая точка входа для интеграции "своего" ИИ-агента:

1. Зарегистрировать бота в портале (один раз) — :meth:`ChatBotService.register_bot`.
2. Реализовать :class:`src.domain.interfaces.agent_handler.AgentHandler` и
   подключить его — :meth:`ChatBotService.register_agent`.
3. Поднять HTTP-приём событий
   (:mod:`src.infrastructure.bot.webhook_server`) и передавать в него входящие
   запросы Bitrix24 методом :meth:`ChatBotService.dispatch_event`.

Сервис сам не хранит бизнес-логику ответов — он только маршрутизирует события
от Bitrix24 к зарегистрированным агентам и отправляет их ответы обратно.
"""

from src.domain.entities.bot_event import (
    BotEventType,
    BotIncomingMessage,
    BotReply,
)
from src.domain.interfaces.agent_handler import AgentHandler
from src.infrastructure.bitrix.imbot_client import ImBotClient
from src.infrastructure.logging.logger import logger


class ChatBotService:
    """Сервис регистрации чат-ботов и маршрутизации событий к агентам."""

    def __init__(self, imbot_client: ImBotClient):
        """Инициализация сервиса.

        :param imbot_client: Клиент для вызова методов ``imbot``/таймлайна.
        """
        self._imbot_client = imbot_client
        self._agents: dict[int, AgentHandler] = {}
        self._bot_codes: dict[str, int] = {}

    async def register_bot(
        self,
        code: str,
        event_handler_url: str,
        name: str,
        work_position: str = "ИИ-помощник",
        openline: bool = True,
    ) -> int | None:
        """Регистрация бота в портале Bitrix24.

        :param code: Уникальный код бота.
        :param event_handler_url: Публичный URL обработчика событий
            (см. :mod:`src.infrastructure.bot.webhook_server`).
        :param name: Имя бота, видимое в чатах.
        :param work_position: Должность/описание бота.
        :param openline: Сделать бота доступным в открытых линиях.
        :return: ID бота в портале или ``None`` при ошибке.
        """
        bot_id = await self._imbot_client.register_bot(
            code=code,
            event_handler_url=event_handler_url,
            name=name,
            work_position=work_position,
            openline=openline,
        )
        if bot_id is not None:
            self._bot_codes[code] = bot_id
        return bot_id

    def register_agent(self, bot_id: int, agent: AgentHandler) -> None:
        """Подключение реализации агента к уже зарегистрированному боту.

        Это единственный шаг, который нужно сделать, чтобы "свой" ИИ-агент
        начал отвечать в чате: дальше сервис сам вызывает
        ``agent.handle_message`` на каждое входящее сообщение этого бота.

        :param bot_id: ID бота, полученный от :meth:`register_bot`.
        :param agent: Объект, реализующий :class:`AgentHandler`.
        """
        self._agents[bot_id] = agent
        logger.info("Агент подключён к боту", bot_id=bot_id, agent=type(agent).__name__)

    async def dispatch_event(self, event: dict) -> None:
        """Обработка одного "сырого" события Bitrix24.

        Парсит событие, находит подключённого агента по ``BOT_ID`` и
        отправляет его ответ обратно в чат (и, если задано, в таймлайн CRM).

        :param event: Словарь события в формате, который шлёт Bitrix24
            (``{"event": "ONIMBOTMESSAGEADD", "data": {...}, "auth": {...}}``).
        """
        event_type = event.get("event")
        if event_type != BotEventType.MESSAGE_ADD.value:
            logger.debug("Пропуск необрабатываемого события", event_type=event_type)
            return

        message = self._parse_message_event(event)
        if message is None:
            return

        agent = self._agents.get(message.bot_id)
        if agent is None:
            logger.warning(
                "Нет агента, подключённого к боту — сообщение проигнорировано",
                bot_id=message.bot_id,
            )
            return

        reply = await agent.handle_message(message)
        await self._send_reply(message, reply)

    @staticmethod
    def _parse_message_event(event: dict) -> BotIncomingMessage | None:
        """Преобразование сырого события Bitrix24 в :class:`BotIncomingMessage`.

        :param event: Сырое событие.
        :return: Нормализованное сообщение или ``None``, если формат не распознан.
        """
        data = event.get("data", {})
        bot_data = data.get("BOT", {})
        message_data = data.get("MESSAGE", {})
        user_data = data.get("USER", {})

        bot_id = bot_data.get("BOT_ID") or bot_data.get("ID")
        dialog_id = message_data.get("DIALOG_ID") or message_data.get("dialog_id")
        text = message_data.get("MESSAGE") or message_data.get("text") or ""

        if bot_id is None or dialog_id is None:
            logger.warning("Не удалось распарсить событие чат-бота", event=event)
            return None

        return BotIncomingMessage(
            bot_id=int(bot_id),
            dialog_id=str(dialog_id),
            user_id=_safe_int(user_data.get("ID")),
            text=text,
            chat_id=_safe_int(message_data.get("CHAT_ID")),
            raw=event,
        )

    async def _send_reply(
        self,
        message: BotIncomingMessage,
        reply: BotReply,
    ) -> None:
        """Отправка ответа агента обратно в Bitrix24.

        :param message: Исходное входящее сообщение (для ``bot_id``/``dialog_id``).
        :param reply: Ответ, сформированный агентом.
        """
        await self._imbot_client.send_message(
            bot_id=message.bot_id,
            dialog_id=message.dialog_id,
            message=reply.text,
            attach=reply.attach or None,
        )

        if reply.timeline_comment is not None:
            await self._imbot_client.add_timeline_comment(
                author_id=message.bot_id,
                entity_type=reply.timeline_comment.entity_type,
                entity_id=reply.timeline_comment.entity_id,
                comment=reply.text,
            )


def _safe_int(value: object) -> int | None:
    """Безопасное приведение значения к ``int``.

    :param value: Произвольное значение из события Bitrix24.
    :return: Целое число или ``None``, если приведение невозможно.
    """
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
