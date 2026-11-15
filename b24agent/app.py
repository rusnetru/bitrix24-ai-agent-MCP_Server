"""Сборка зависимостей приложения."""

from __future__ import annotations

from b24agent.bitrix import Bitrix
from b24agent.config import Settings
from b24agent.entities import EntityRegistry
from b24agent.store import Store
from b24agent.tools import Toolbox


class App:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.b24 = Bitrix(settings.webhook_url, settings.verify_ssl, settings.bitrix_timeout)
        self.entities = EntityRegistry(self.b24)
        self.store = Store(settings.data_dir)
        self.toolbox = Toolbox()

    async def close(self) -> None:
        await self.b24.close()
