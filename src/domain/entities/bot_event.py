"""Доменные сущности для событий чат-бота Bitrix24.

Содержат входящие сообщения/события от Битрикс24 и ответы агента,
не привязанные к конкретному транспорту (HTTP-вебхук, fetch-режим и т.д.).
"""

from dataclasses import dataclass, field
from enum import Enum


class BotEventType(str, Enum):
    """Типы событий чат-бота, которые умеет обрабатывать сервис."""

    MESSAGE_ADD = "ONIMBOTMESSAGEADD"
    JOIN_CHAT = "ONIMBOTJOINCHAT"
    WELCOME_MESSAGE = "ONIMBOTWELCOMEMESSAGE"
    BOT_DELETE = "ONIMBOTDELETE"
    COMMAND_ADD = "ONIMBOTCOMMANDADD"
    TIMELINE_COMMENT_ADD = "ONCRMTIMELINECOMMENTADD"


@dataclass(slots=True)
class BotIncomingMessage:
    """Входящее сообщение для бота, нормализованное из события Bitrix24.

    :param bot_id: ID бота-получателя в портале.
    :param dialog_id: Идентификатор диалога/чата (``DIALOG_ID``).
    :param chat_id: Числовой ID чата, если есть.
    :param user_id: ID пользователя-отправителя (менеджера).
    :param text: Текст сообщения.
    :param command: Имя вызванной команды бота, если сообщение — команда.
    :param raw: Исходные данные события для расширенной обработки.
    """

    bot_id: int
    dialog_id: str
    user_id: int | None
    text: str
    chat_id: int | None = None
    command: str | None = None
    raw: dict = field(default_factory=dict)


@dataclass(slots=True)
class BotReply:
    """Ответ агента, который нужно отправить обратно в Bitrix24.

    :param text: Текст ответа.
    :param attach: Rich-вложения (карточки, кнопки) для ``imbot.message.add``.
    :param timeline_comment: Если задано — дополнительно продублировать ответ
        комментарием в таймлайне CRM-сущности (например, для истории по сделке).
    """

    text: str
    attach: list[dict] = field(default_factory=list)
    timeline_comment: "TimelineCommentTarget | None" = None


@dataclass(slots=True)
class TimelineCommentTarget:
    """Параметры публикации комментария в таймлайне CRM от имени бота.

    :param entity_type: Тип CRM-сущности (``lead``, ``deal``, ``contact``, ``company``).
    :param entity_id: ID сущности.
    """

    entity_type: str
    entity_id: int
