"""Настройки из переменных окружения / файла .env."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == '':
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on', 'да'}


def _ssl(name: str) -> bool | str:
    """true/false или путь к CA-бандлу (для самоподписанных сертификатов коробки)."""
    raw = (os.getenv(name) or 'true').strip()
    if raw.lower() in {'0', 'false', 'no', 'off', 'нет'}:
        return False
    if raw.lower() in {'1', 'true', 'yes', 'on', 'да'}:
        return True
    return raw


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw and raw.strip() else default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw and raw.strip() else default


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name) or ''
    return frozenset(int(x) for x in raw.replace(';', ',').split(',') if x.strip().isdigit())


@dataclass(frozen=True)
class Settings:
    # --- Bitrix24 (коробка) ---
    webhook_url: str
    portal_url: str
    verify_ssl: bool | str = True
    bitrix_timeout: float = 60

    # --- LLM через LiteLLM (OpenAI-совместимый API) ---
    llm_base_url: str = ''
    llm_api_key: str = ''
    llm_model: str = ''
    llm_tool_mode: str = 'auto'  # auto | native | json
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_timeout: float = 180
    llm_verify_ssl: bool | str = True
    llm_extra_body: dict = field(default_factory=dict)

    # --- Чат-бот ---
    bot_code: str = 'b24_ai_agent'
    bot_name: str = 'ИИ-агент'
    bot_last_name: str = ''
    bot_work_position: str = 'ИИ-помощник по CRM и задачам'
    bot_color: str = 'AQUA'
    bot_client_id: str = 'b24_ai_agent_client'
    bot_openline: bool = False

    # --- HTTP-приёмник событий ---
    public_url: str = ''
    event_secret: str = ''
    http_host: str = '0.0.0.0'
    http_port: int = 8090

    # --- Поведение агента ---
    allow_write: bool = True
    allowed_users: frozenset[int] = frozenset()
    history_limit: int = 20
    max_steps: int = 10
    extra_prompt: str = ''
    timeline_reply_mode: str = 'watched'  # watched: упоминание + все комментарии отслеживаемых объектов; mention: только упоминание
    timeline_poll_interval: int = 0

    data_dir: Path = Path('data')
    log_level: str = 'INFO'

    @property
    def events_url(self) -> str:
        """URL, который Битрикс24 будет вызывать при событиях."""
        base = self.public_url.rstrip('/')
        url = f'{base}/bitrix/events'
        if self.event_secret:
            url += f'?secret={self.event_secret}'
        return url


def load_settings(env_file: str | None = None) -> Settings:
    load_dotenv(env_file or Path.cwd() / '.env', override=False)

    webhook = (os.getenv('BITRIX_WEBHOOK_URL') or os.getenv('BITRIX24_WEBHOOK_URL') or '').strip()
    if not webhook:
        msg = (
            'Не задан BITRIX_WEBHOOK_URL. Создайте входящий вебхук в коробке: '
            'Приложения → Разработчикам → Другое → Входящий вебхук '
            '(права: crm, task, user, im, imbot) и укажите URL вида '
            'https://portal.company.ru/rest/1/xxxxxxxx/'
        )
        raise RuntimeError(msg)
    webhook = webhook.rstrip('/') + '/'
    parts = urlsplit(webhook)
    portal = (os.getenv('BITRIX_PORTAL_URL') or f'{parts.scheme}://{parts.netloc}').rstrip('/')

    extra_raw = os.getenv('LLM_EXTRA_BODY') or ''
    extra = json.loads(extra_raw) if extra_raw.strip() else {}

    prompt_file = os.getenv('AGENT_PROMPT_FILE') or ''
    extra_prompt = Path(prompt_file).read_text(encoding='utf-8') if prompt_file else ''

    bot_code = os.getenv('BOT_CODE') or 'b24_ai_agent'
    return Settings(
        webhook_url=webhook,
        portal_url=portal,
        verify_ssl=_ssl('BITRIX_VERIFY_SSL'),
        bitrix_timeout=_float('BITRIX_TIMEOUT', 60),
        llm_base_url=(os.getenv('LLM_BASE_URL') or '').rstrip('/'),
        llm_api_key=os.getenv('LLM_API_KEY') or '',
        llm_model=os.getenv('LLM_MODEL') or '',
        llm_tool_mode=(os.getenv('LLM_TOOL_MODE') or 'auto').lower(),
        llm_temperature=_float('LLM_TEMPERATURE', 0.2),
        llm_max_tokens=_int('LLM_MAX_TOKENS', 2048),
        llm_timeout=_float('LLM_TIMEOUT', 180),
        llm_verify_ssl=_ssl('LLM_VERIFY_SSL'),
        llm_extra_body=extra,
        bot_code=bot_code,
        bot_name=os.getenv('BOT_NAME') or 'ИИ-агент',
        bot_last_name=os.getenv('BOT_LAST_NAME') or '',
        bot_work_position=os.getenv('BOT_WORK_POSITION') or 'ИИ-помощник по CRM и задачам',
        bot_color=os.getenv('BOT_COLOR') or 'AQUA',
        bot_client_id=os.getenv('BOT_CLIENT_ID') or f'{bot_code}_client',
        bot_openline=_bool('BOT_OPENLINE', False),
        public_url=(os.getenv('PUBLIC_URL') or '').rstrip('/'),
        event_secret=os.getenv('EVENT_SECRET') or '',
        http_host=os.getenv('HTTP_HOST') or '0.0.0.0',
        http_port=_int('HTTP_PORT', 8090),
        allow_write=_bool('AGENT_ALLOW_WRITE', True),
        allowed_users=_ids('AGENT_ALLOWED_USERS'),
        history_limit=_int('AGENT_HISTORY', 20),
        max_steps=_int('AGENT_MAX_STEPS', 10),
        extra_prompt=extra_prompt,
        timeline_reply_mode=(os.getenv('TIMELINE_REPLY_MODE') or 'watched').lower(),
        timeline_poll_interval=_int('TIMELINE_POLL_INTERVAL', 0),
        data_dir=Path(os.getenv('DATA_DIR') or 'data'),
        log_level=(os.getenv('LOG_LEVEL') or 'INFO').upper(),
    )
