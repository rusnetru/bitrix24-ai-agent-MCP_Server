"""HTTP-сервер для приёма событий чат-бота Bitrix24.

Bitrix24 шлёт события (``ONIMBOTMESSAGEADD`` и т.д.) как
``application/x-www-form-urlencoded`` POST-запросы с "плоскими" ключами вида
``data[MESSAGE][text]``. Этот модуль разворачивает такие ключи в вложенный
словарь и передаёт результат в :class:`ChatBotService.dispatch_event`.

Запускается отдельно от stdio MCP-сервера (см. ``src/bot_main.py``), так как
MCP работает по stdio, а вебхукам нужен отдельный публичный HTTP-порт.
"""

from aiohttp import web

from src.application.services.chat_bot import ChatBotService
from src.infrastructure.logging.logger import logger


def create_webhook_app(chat_bot_service: ChatBotService) -> web.Application:
    """Создание aiohttp-приложения с эндпоинтом приёма событий бота.

    :param chat_bot_service: Сервис маршрутизации событий к агентам.
    :return: Настроенное приложение aiohttp.
    """
    app = web.Application()
    app["chat_bot_service"] = chat_bot_service
    app.router.add_post("/bot/events", _handle_bot_event)
    app.router.add_post("/bot/command", _handle_bot_event)
    return app


async def _handle_bot_event(request: web.Request) -> web.Response:
    """Приём одного события от Bitrix24 и передача его в сервис чат-бота.

    Всегда отвечает ``200 OK`` сразу после постановки события на обработку,
    как требует Bitrix24 (иначе портал считает обработчик недоступным и
    повторяет запрос).

    :param request: Входящий HTTP-запрос aiohttp.
    :return: Пустой ответ ``200 OK``.
    """
    chat_bot_service: ChatBotService = request.app["chat_bot_service"]

    try:
        form = await request.post()
        event = _unflatten(dict(form))
    except (ValueError, TypeError):
        logger.exception("Не удалось разобрать событие чат-бота")
        return web.Response(status=200)

    try:
        await chat_bot_service.dispatch_event(event)
    except Exception:
        logger.exception("Ошибка обработки события чат-бота", event=event)

    return web.Response(status=200)


def _unflatten(form: dict[str, str]) -> dict:
    """Разворачивание "плоских" ключей формы Bitrix24 (``a[b][c]``) в словарь.

    :param form: Плоский словарь параметров формы.
    :return: Вложенный словарь, эквивалентный исходному JSON-событию.
    """
    result: dict = {}
    for flat_key, value in form.items():
        parts = [p.rstrip("]") for p in flat_key.replace("[", " ").split(" ") if p]
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
    return result


def run_webhook_server(
    chat_bot_service: ChatBotService,
    host: str = "0.0.0.0",  # noqa: S104 - сервер обработчика веб-хуков обязан слушать внешние запросы
    port: int = 8090,
) -> None:
    """Запуск HTTP-сервера приёма событий бота (блокирующий вызов).

    :param chat_bot_service: Сервис маршрутизации событий к агентам.
    :param host: Адрес для прослушивания.
    :param port: Порт для прослушивания.
    """
    app = create_webhook_app(chat_bot_service)
    logger.info("Запуск HTTP-сервера приёма событий чат-бота", host=host, port=port)
    web.run_app(app, host=host, port=port)
