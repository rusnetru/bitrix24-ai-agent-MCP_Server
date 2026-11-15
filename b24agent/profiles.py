"""Профили агентов: универсальный агент (свои права) и личные ассистенты сотрудников (права сотрудника).

- **Универсальный агент** — сервисная учётка, вебхук ``BITRIX_WEBHOOK_URL``. Работает в бизнес-процессах,
  автопомощи, групповых чатах, таймлайне, MCP по умолчанию.
- **Личный ассистент** — вебхук, созданный ОТ ИМЕНИ сотрудника: агент видит и делает ровно то, что разрешено
  этому сотруднику в Битрикс24, и действует от его имени. У ассистента личная память о сотруднике.

Личные ассистенты описываются в файле ``PERSONAL_AGENTS_FILE`` (TOML, по умолчанию personal_agents.toml)::

    [[agent]]
    user_id = 15
    name = "Ассистент Анны"
    webhook_env = "PA_15_WEBHOOK"   # или webhook = "https://portal/rest/15/xxxx/"
    own_bot = false                 # true — отдельный скрытый бот «Ассистент Анны» только для неё
    allow_write = true
    memory = true
    prompt = "Обращайся на «ты», отвечай кратко"   # или prompt_file = "prompts/anna.md"
    tools_disabled = ["bp_start"]
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings

log = logging.getLogger(__name__)


@dataclass
class AgentProfile:
    key: str  # 'service' | 'user:15'
    kind: str  # 'service' | 'personal'
    b24: Bitrix
    settings: Settings  # настройки с учётом профиля (allow_write, tools_disabled, extra_prompt)
    user_id: int | None = None  # владелец личного ассистента
    name: str = ''
    memory: bool = False
    own_bot: bool = False
    ok: bool = True  # прошёл проверку вебхука
    problem: str = ''

    @property
    def personal(self) -> bool:
        return self.kind == 'personal'


@dataclass
class ProfileRegistry:
    service: AgentProfile
    personal: dict[int, AgentProfile] = field(default_factory=dict)

    def for_user(self, user_id: int | None) -> AgentProfile | None:
        p = self.personal.get(user_id) if user_id else None
        return p if p and p.ok else None

    def all(self) -> list[AgentProfile]:
        return [self.service, *self.personal.values()]

    async def close(self) -> None:
        for p in self.personal.values():
            await p.b24.close()


def _read_prompt(entry: dict, base: Path) -> str:
    if entry.get('prompt_file'):
        path = Path(entry['prompt_file'])
        return (path if path.is_absolute() else base / path).read_text(encoding='utf-8')
    return str(entry.get('prompt') or '')


def load_profiles(settings: Settings, service_b24: Bitrix) -> ProfileRegistry:
    service = AgentProfile(key='service', kind='service', b24=service_b24, settings=settings,
                           name=settings.bot_name, memory=False)
    reg = ProfileRegistry(service=service)
    path = Path(settings.personal_agents_file)
    if not path.is_file():
        return reg
    data = tomllib.loads(path.read_text(encoding='utf-8'))
    for entry in data.get('agent', []):
        uid = int(entry['user_id'])
        webhook = entry.get('webhook') or (os.getenv(entry['webhook_env']) if entry.get('webhook_env') else '')
        s = replace(
            settings,
            allow_write=bool(entry.get('allow_write', settings.allow_write)),
            tools_disabled=settings.tools_disabled | frozenset(entry.get('tools_disabled', [])),
            extra_prompt='\n\n'.join(x for x in (settings.extra_prompt, _read_prompt(entry, path.parent)) if x),
        )
        profile = AgentProfile(
            key=f'user:{uid}', kind='personal', user_id=uid, settings=s,
            b24=Bitrix(webhook, settings.verify_ssl, settings.bitrix_timeout) if webhook else service_b24,
            name=str(entry.get('name') or f'Личный ассистент #{uid}'),
            memory=bool(entry.get('memory', True)), own_bot=bool(entry.get('own_bot', False)),
            ok=bool(webhook), problem='' if webhook else 'не задан webhook / webhook_env',
        )
        if webhook and profile.b24.webhook_user_id != uid:
            profile.ok = False
            profile.problem = (f'вебхук создан пользователем {profile.b24.webhook_user_id}, а не {uid} — '
                               'личный ассистент должен работать через вебхук самого сотрудника')
        reg.personal[uid] = profile
    log.info('Личных ассистентов в конфигурации: %s', len(reg.personal))
    return reg


async def verify(profile: AgentProfile) -> AgentProfile:
    """Проверить, что вебхук работает и принадлежит владельцу профиля (метод profile)."""
    if not profile.ok:
        return profile
    try:
        me = await profile.b24.call('profile')
    except BitrixError as exc:
        profile.ok, profile.problem = False, f'вебхук не работает: {exc.code} {exc.description}'
        return profile
    if profile.personal and int(me.get('ID') or 0) != profile.user_id:
        profile.ok, profile.problem = False, f'вебхук принадлежит пользователю {me.get("ID")}, а не {profile.user_id}'
    return profile
