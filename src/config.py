"""Модуль конфигурации приложения.

Предоставляет настройки приложения через переменные окружения.
"""

import os
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class Settings:
    """Настройки приложения.

    :param bitrix_webhook_url: URL вебхука Bitrix24.
    :param log_level: Уровень логирования (DEBUG, INFO, WARNING, ERROR, CRITICAL).
    :param bot_code: Уникальный код чат-бота для регистрации в Bitrix24.
    :param bot_name: Отображаемое имя чат-бота в интерфейсе Bitrix24.
    :param bot_event_handler_url: Публичный URL, на который Bitrix24 будет
        слать события чат-бота (должен указывать на ``/bot/events`` этого сервера).
    :param bot_http_host: Адрес, на котором поднимается HTTP-сервер вебхуков бота.
    :param bot_http_port: Порт HTTP-сервера вебхуков бота.
    """

    BITRIX_WEBHOOK_URL: str
    LOG_LEVEL: str = "INFO"
    BOT_CODE: str = "ai_assistant"
    BOT_NAME: str = "AI Ассистент"
    BOT_EVENT_HANDLER_URL: str = ""
    BOT_HTTP_HOST: str = "0.0.0.0"  # noqa: S104
    BOT_HTTP_PORT: int = 8090


class SettingsManager:
    """Менеджер настроек приложения.
    Реализует паттерн Singleton для управления настройками.
    """

    _instance: ClassVar[Settings | None] = None

    @classmethod
    def init(cls) -> Settings:
        """Инициализация настроек приложения.

        :return: Экземпляр настроек.
        :raises ValueError: Если не указан URL вебхука.
        """
        if cls._instance is None:
            webhook_url = os.getenv("BITRIX_WEBHOOK_URL")
            log_level = os.getenv("LOG_LEVEL", "INFO")

            if not webhook_url:
                msg = (
                    "Не указана переменная окружения BITRIX_WEBHOOK_URL. "
                    "Пожалуйста, укажите URL вебхука Bitrix24.",
                )
                raise ValueError(msg)

            cls._instance = Settings(
                BITRIX_WEBHOOK_URL=webhook_url,
                LOG_LEVEL=log_level,
                BOT_CODE=os.getenv("BOT_CODE", "ai_assistant"),
                BOT_NAME=os.getenv("BOT_NAME", "AI Ассистент"),
                BOT_EVENT_HANDLER_URL=os.getenv("BOT_EVENT_HANDLER_URL", ""),
                BOT_HTTP_HOST=os.getenv("BOT_HTTP_HOST", "0.0.0.0"),  # noqa: S104
                BOT_HTTP_PORT=int(os.getenv("BOT_HTTP_PORT", "8090")),
            )
        return cls._instance

    @classmethod
    def get(cls) -> Settings:
        """Получение текущих настроек приложения.

        :return: Экземпляр настроек.
        """
        if cls._instance is None:
            return cls.init()
        return cls._instance
