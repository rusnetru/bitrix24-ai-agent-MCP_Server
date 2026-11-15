"""Справочник типов CRM-объектов: стандартные сущности и смарт-процессы."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from b24agent.bitrix import Bitrix


@dataclass(frozen=True)
class EntityType:
    id: int  # entityTypeId
    code: str  # LEAD / DEAL / CONTACT / COMPANY / QUOTE / SMART_INVOICE / DYNAMIC_1038
    title: str
    is_dynamic: bool = False
    categories_enabled: bool = True
    stages_enabled: bool = True

    @property
    def timeline_type(self) -> str:
        """Значение ENTITY_TYPE для crm.timeline.comment.*"""
        return f'dynamic_{self.id}' if self.is_dynamic else self.code.lower()

    @property
    def task_prefix(self) -> str:
        """Префикс привязки задачи (UF_CRM_TASK): D_12, C_5, T80_7 ..."""
        fixed = {1: 'L', 2: 'D', 3: 'C', 4: 'CO', 7: 'Q', 31: 'SI'}
        return fixed.get(self.id) or f'T{self.id:x}'

    def url(self, portal: str, item_id: int) -> str:
        paths = {1: 'lead', 2: 'deal', 3: 'contact', 4: 'company', 7: 'quote'}
        if self.id in paths:
            return f'{portal}/crm/{paths[self.id]}/details/{item_id}/'
        return f'{portal}/crm/type/{self.id}/details/{item_id}/'


STANDARD = {
    1: EntityType(1, 'LEAD', 'Лид', categories_enabled=False),
    2: EntityType(2, 'DEAL', 'Сделка'),
    3: EntityType(3, 'CONTACT', 'Контакт', stages_enabled=False),
    4: EntityType(4, 'COMPANY', 'Компания', stages_enabled=False),
    7: EntityType(7, 'QUOTE', 'Предложение', categories_enabled=False),
    31: EntityType(31, 'SMART_INVOICE', 'Счёт'),
}

ALIASES = {
    'lead': 1, 'лид': 1, 'лиды': 1,
    'deal': 2, 'сделка': 2, 'сделки': 2,
    'contact': 3, 'контакт': 3, 'контакты': 3,
    'company': 4, 'компания': 4, 'компании': 4,
    'quote': 7, 'предложение': 7, 'коммерческое предложение': 7,
    'invoice': 31, 'smart_invoice': 31, 'счёт': 31, 'счет': 31,
}

# Префиксы привязок в задачах (UF_CRM_TASK) → entityTypeId
TASK_PREFIX = {'L': 1, 'D': 2, 'C': 3, 'CO': 4, 'Q': 7, 'SI': 31}


class EntityRegistry:
    def __init__(self, b24: Bitrix, ttl: int = 600):
        self._b24 = b24
        self._ttl = ttl
        self._dynamic: dict[int, EntityType] = {}
        self._loaded_at = 0.0

    async def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._loaded_at < self._ttl:
            return
        types, _ = await self._b24.list('crm.type.list', {}, limit=500, key='types')
        self._dynamic = {
            int(t['entityTypeId']): EntityType(
                id=int(t['entityTypeId']),
                code=f'DYNAMIC_{t["entityTypeId"]}',
                title=t.get('title') or f'Смарт-процесс {t["entityTypeId"]}',
                is_dynamic=True,
                categories_enabled=str(t.get('isCategoriesEnabled', 'Y')) in {'Y', 'True', 'true', '1'},
                stages_enabled=str(t.get('isStagesEnabled', 'Y')) in {'Y', 'True', 'true', '1'},
            )
            for t in types
            if t.get('entityTypeId')
        }
        self._loaded_at = time.monotonic()

    async def all(self) -> list[EntityType]:
        await self.refresh()
        return [*STANDARD.values(), *self._dynamic.values()]

    async def get(self, ref: str | int) -> EntityType:
        """Найти тип по entityTypeId, коду (DEAL, DYNAMIC_1038), алиасу или названию смарт-процесса."""
        await self.refresh()
        if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
            etid = int(ref)
            if etid in STANDARD:
                return STANDARD[etid]
            if etid not in self._dynamic:
                await self.refresh(force=True)
            if etid in self._dynamic:
                return self._dynamic[etid]
            msg = f'Неизвестный entityTypeId={etid}'
            raise ValueError(msg)
        text = str(ref).strip()
        low = text.lower()
        if low in ALIASES:
            return STANDARD[ALIASES[low]]
        m = re.fullmatch(r'(?:dynamic_|t)?(\d+)', low)
        if m:
            return await self.get(int(m.group(1)))
        for et in STANDARD.values():
            if et.code.lower() == low:
                return et
        for et in self._dynamic.values():
            if et.title.lower() == low:
                return et
        for et in self._dynamic.values():
            if low in et.title.lower():
                return et
        names = ', '.join(f'{e.title} ({e.id})' for e in await self.all())
        msg = f'Тип объекта «{text}» не найден. Доступны: {names}'
        raise ValueError(msg)

    async def from_timeline_type(self, value: str) -> EntityType:
        return await self.get(value.replace('dynamic_', ''))

    async def from_chat_entity(self, value: str) -> tuple[EntityType, int] | None:
        """Разбор CHAT_ENTITY_ID вида ``DEAL|123`` / ``DYNAMIC_1038|5`` / ``2|123``."""
        if not value or '|' not in value:
            return None
        type_part, _, id_part = value.partition('|')
        id_part = id_part.split('|')[0]
        if not id_part.isdigit():
            return None
        try:
            return await self.get(type_part), int(id_part)
        except ValueError:
            return None

    async def from_task_binding(self, value: str) -> tuple[EntityType, int] | None:
        m = re.fullmatch(r'([A-Z]+)([0-9a-f]*)_(\d+)', value)
        if not m:
            return None
        prefix, hexid, item = m.groups()
        try:
            if prefix == 'T' and hexid:
                return await self.get(int(hexid, 16)), int(item)
            if prefix in TASK_PREFIX:
                return STANDARD[TASK_PREFIX[prefix]], int(item)
        except ValueError:
            return None
        return None
