"""Модуль с контейнером зависимостей приложения.

Предоставляет единую точку инициализации и получения зависимостей.
"""

from dishka import Provider, Scope, make_container, provide
from fast_bitrix24 import Bitrix

from src.application.services.chat_bot import ChatBotService
from src.application.services.contact import ContactService
from src.application.services.deal import DealService
from src.config import SettingsManager
from src.infrastructure.bitrix.bitrix_contact_repository import BitrixContactRepository
from src.infrastructure.bitrix.bitrix_deal_repository import BitrixDealRepository
from src.infrastructure.bitrix.imbot_client import ImBotClient
from src.infrastructure.bitrix.repository_factory import BitrixRepositoryFactory
from src.infrastructure.logging.logger import logger
from src.infrastructure.mcp.server import BitrixMCPServer


class DependencyProvider(Provider):
    """Главный провайдер зависимостей приложения."""

    def __init__(self) -> None:
        super().__init__()
        self.bitrix_webhook_url = SettingsManager.get().BITRIX_WEBHOOK_URL

    @provide(scope=Scope.APP)
    def provide_bitrix_webhook_url(self) -> str:
        """Предоставляет URL вебхука Bitrix."""
        return self.bitrix_webhook_url

    @provide(scope=Scope.APP)
    def provide_bitrix_client(self) -> Bitrix:
        """Предоставляет единый клиент REST API Bitrix24.

        :return: Экземпляр клиента Bitrix24.
        """
        return Bitrix(self.bitrix_webhook_url)

    @provide(scope=Scope.APP)
    def provide_repository_factory(self, bitrix_client: Bitrix) -> BitrixRepositoryFactory:
        """Создание фабрики репозиториев Bitrix24.

        :param bitrix_client: Клиент REST API Bitrix24.
        :return: Экземпляр фабрики репозиториев
        """
        logger.info("Инициализация фабрики репозиториев Bitrix24")
        contact_repository = BitrixContactRepository(bitrix_client)
        return BitrixRepositoryFactory(
            [
                contact_repository,
                BitrixDealRepository(bitrix_client, contact_repository),
            ],
        )

    @provide(scope=Scope.APP)
    def provide_imbot_client(self, bitrix_client: Bitrix) -> ImBotClient:
        """Предоставляет клиент методов чат-ботов (``imbot``) Bitrix24.

        :param bitrix_client: Клиент REST API Bitrix24.
        :return: Экземпляр клиента чат-бота.
        """
        return ImBotClient(bitrix_client)

    @provide(scope=Scope.APP)
    def provide_chat_bot_service(self, imbot_client: ImBotClient) -> ChatBotService:
        """Предоставляет сервис подключения ИИ-агентов к чатам Bitrix24.

        :param imbot_client: Клиент методов чат-бота.
        :return: Экземпляр сервиса чат-бота.
        """
        return ChatBotService(imbot_client)

    @provide(scope=Scope.APP)
    def provide_mcp_server(self) -> BitrixMCPServer:
        """Предоставляет сервер MCP."""
        return BitrixMCPServer()

    @provide(scope=Scope.APP)
    def provide_contact_service(self, repository_factory: BitrixRepositoryFactory) -> ContactService:
        """Предоставляет сервис для работы с контактами."""
        return ContactService(repository_factory)

    @provide(scope=Scope.APP)
    def provide_deal_service(self, repository_factory: BitrixRepositoryFactory) -> DealService:
        """Предоставляет сервис для работы со сделками."""
        return DealService(repository_factory)


provider = DependencyProvider()
container = make_container(provider)
