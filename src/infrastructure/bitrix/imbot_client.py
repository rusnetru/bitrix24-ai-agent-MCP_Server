"""Клиент для методов чат-ботов Bitrix24 (``imbot``) и комментариев таймлайна.

Тонкая обёртка над :class:`fast_bitrix24.Bitrix`, инкапсулирующая вызовы REST
методов, необходимых для подключения внешнего ИИ-агента к чатам и таймлайну
Bitrix24 (см. ``docs/source/ai_chat_agent.rst``).
"""

from fast_bitrix24 import Bitrix

from src.infrastructure.logging.logger import logger


class ImBotClient:
    """Обёртка над REST-методами ``imbot.*`` и ``crm.timeline.comment.add``."""

    def __init__(self, bitrix: Bitrix):
        """Инициализация клиента.

        :param bitrix: Клиент для работы с REST API Bitrix24.
        """
        self._bitrix = bitrix

    async def register_bot(  # noqa: PLR0913 - параметры отражают поля imbot.register
        self,
        code: str,
        event_handler_url: str,
        name: str,
        work_position: str = "ИИ-помощник",
        openline: bool = True,
        color: str = "AQUA",
    ) -> int | None:
        """Регистрация чат-бота в портале (``imbot.register``).

        Делает бота доступным как участника обычных чатов (и открытых линий,
        если ``openline=True``); ID бота далее используется как ``AUTHOR_ID``
        для комментариев в таймлайне CRM — для Bitrix24 он выглядит как
        обычный пользователь-сотрудник.

        :param code: Уникальный код бота в рамках приложения.
        :param event_handler_url: URL, на который Bitrix24 будет слать события.
        :param name: Отображаемое имя бота.
        :param work_position: Должность/описание бота в профиле.
        :param openline: Сделать бота доступным в открытых линиях.
        :param color: Цвет аватара бота в интерфейсе Bitrix24.
        :return: ID зарегистрированного бота (он же ID пользователя) или ``None``.
        """
        result = await self._bitrix.call(
            "imbot.register",
            {
                "CODE": code,
                "TYPE": "B",
                "EVENT_HANDLER": event_handler_url,
                "EVENT_MESSAGE_ADD": event_handler_url,
                "EVENT_WELCOME_MESSAGE": event_handler_url,
                "EVENT_BOT_DELETE": event_handler_url,
                "OPENLINE": "Y" if openline else "N",
                "PROPERTIES": {
                    "NAME": name,
                    "WORK_POSITION": work_position,
                    "COLOR": color,
                },
            },
        )
        bot_id = result if isinstance(result, int) else None
        logger.info("Зарегистрирован чат-бот", code=code, bot_id=bot_id)
        return bot_id

    async def unregister_bot(self, bot_id: int) -> bool:
        """Удаление чат-бота из портала (``imbot.unregister``).

        :param bot_id: ID бота.
        :return: Признак успешного выполнения.
        """
        result = await self._bitrix.call("imbot.unregister", {"BOT_ID": bot_id})
        return bool(result)

    async def register_command(
        self,
        bot_id: int,
        command: str,
        event_command_url: str,
        common: bool = True,
    ) -> int | None:
        """Регистрация команды бота (``imbot.command.register``).

        :param bot_id: ID бота-владельца команды.
        :param command: Имя команды (без ведущего ``/``).
        :param event_command_url: URL обработчика вызова команды.
        :param common: Доступна ли команда всем пользователям.
        :return: ID команды или ``None``.
        """
        result = await self._bitrix.call(
            "imbot.command.register",
            {
                "BOT_ID": bot_id,
                "COMMAND": command,
                "COMMON": "Y" if common else "N",
                "EVENT_COMMAND_ADD": event_command_url,
            },
        )
        return result if isinstance(result, int) else None

    async def send_message(
        self,
        bot_id: int,
        dialog_id: str,
        message: str,
        attach: list[dict] | None = None,
    ) -> int | None:
        """Отправка сообщения в чат от имени бота (``imbot.message.add``).

        :param bot_id: ID бота-отправителя.
        :param dialog_id: ID диалога/чата.
        :param message: Текст сообщения.
        :param attach: Rich-вложения (карточки товаров, кнопки и т.д.).
        :return: ID отправленного сообщения или ``None``.
        """
        params = {
            "BOT_ID": bot_id,
            "DIALOG_ID": dialog_id,
            "MESSAGE": message,
        }
        if attach:
            params["ATTACH"] = attach
        result = await self._bitrix.call("imbot.message.add", params)
        return result if isinstance(result, int) else None

    async def add_timeline_comment(
        self,
        author_id: int,
        entity_type: str,
        entity_id: int,
        comment: str,
    ) -> int | None:
        """Публикация комментария в таймлайне CRM от имени бота.

        Использует ``AUTHOR_ID = bot_id``, поэтому в таймлайне комментарий
        выглядит так, как если бы его оставил обычный сотрудник.

        :param author_id: ID бота (используется как автор комментария).
        :param entity_type: Тип CRM-сущности (``lead``, ``deal``, ``contact``, ``company``).
        :param entity_id: ID сущности.
        :param comment: Текст комментария.
        :return: ID созданного комментария или ``None``.
        """
        result = await self._bitrix.call(
            "crm.timeline.comment.add",
            {
                "fields": {
                    "ENTITY_ID": entity_id,
                    "ENTITY_TYPE": entity_type,
                    "AUTHOR_ID": author_id,
                    "COMMENT": comment,
                },
            },
        )
        return result if isinstance(result, int) else None
