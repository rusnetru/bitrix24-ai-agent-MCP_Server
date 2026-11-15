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


def _read(path: str) -> str:
    return Path(path).read_text(encoding='utf-8') if path else ''


def _list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name) or ''
    return tuple(x.strip() for x in raw.replace(';', ',').split(',') if x.strip())


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
    bot_api: str = 'v2'  # v2 — imbot.v2 (рекомендуется) | v1 — устаревший imbot.*
    bot_event_mode: str = 'fetch'  # fetch — агент сам забирает события (закрытый контур) | webhook — push
    bot_token: str = ''  # секрет бота для imbot.v2 через вебхук (произвольная строка, до 40 символов)
    bot_type: str = 'bot'  # v2: bot (личка и @упоминания) | supervisor (все сообщения в своих чатах)
    bot_poll_interval: float = 3
    bot_hidden: bool = False

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
    # Сканирование таймлайнов недавно активных элементов (закрытый контур без событий):
    timeline_scan_types: tuple[str, ...] = ()  # например ('deal', '1038')
    timeline_scan_interval: int = 60
    timeline_mention_ids: frozenset[int] = frozenset()  # кроме бота: ID сервисного пользователя вебхука

    # --- Письма и файлы ---
    files_max_mb: int = 25
    file_read_chars: int = 12000
    # Автопомощь: по новым письмам/файлам у элементов этих типов агент сам готовит справку
    auto_assist_types: tuple[str, ...] = ()
    auto_assist_on: frozenset[str] = frozenset({'email', 'file'})  # email | file
    auto_assist_target: frozenset[str] = frozenset({'timeline'})  # timeline | chat (ответственному в личку)
    auto_assist_interval: int = 120
    auto_assist_max: int = 10  # не больше N справок за один проход
    auto_assist_prompt: str = ''

    # --- Участник бизнес-процессов ---
    bp_agent: bool = False
    bp_poll_interval: int = 60
    bp_decisions: frozenset[str] = frozenset({'approve', 'reject', 'ok', 'cancel'})
    bp_min_confidence: float = 0.5
    bp_escalate_to: int | None = None
    bp_max_per_cycle: int = 5
    bp_timeline_note: bool = True
    bp_extra_prompt: str = ''

    # --- Личные ассистенты (права сотрудника) ---
    personal_agents_file: str = 'personal_agents.toml'
    # Личный чат с универсальным ботом: service — права агента; personal — если у сотрудника есть личный
    # ассистент, то его права и память, иначе права агента; personal_only — без личного ассистента отказ
    private_chat_mode: str = 'personal'
    personal_memory_max: int = 50
    personal_history: int = 40

    # --- Инструменты и расширения ---
    tools_disabled: frozenset[str] = frozenset()
    plugins: tuple[str, ...] = ()
    plugins_dir: Path = Path('plugins')
    bitrix_call: str = 'read'  # off | read | full
    bitrix_call_allow: tuple[str, ...] = ()
    bitrix_call_deny: tuple[str, ...] = ()

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
            '(права: crm, task, user, im, imbot, department, sonet, calendar, bizproc, lists) и укажите URL вида '
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
        bot_api=(os.getenv('BOT_API') or 'v2').lower(),
        bot_event_mode=(os.getenv('BOT_EVENT_MODE') or 'fetch').lower(),
        bot_token=os.getenv('BOT_TOKEN') or '',
        bot_type=(os.getenv('BOT_TYPE') or 'bot').lower(),
        bot_poll_interval=_float('BOT_POLL_INTERVAL', 3),
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
        timeline_scan_types=_list('TIMELINE_SCAN_TYPES'),
        timeline_scan_interval=_int('TIMELINE_SCAN_INTERVAL', 60),
        timeline_mention_ids=_ids('TIMELINE_MENTION_IDS'),
        files_max_mb=_int('FILES_MAX_MB', 25),
        file_read_chars=_int('FILE_READ_CHARS', 12000),
        auto_assist_types=_list('AUTO_ASSIST_TYPES'),
        auto_assist_on=frozenset(x.lower() for x in (_list('AUTO_ASSIST_ON') or ('email', 'file'))),
        auto_assist_target=frozenset(x.lower() for x in (_list('AUTO_ASSIST_TARGET') or ('timeline',))),
        auto_assist_interval=_int('AUTO_ASSIST_INTERVAL', 120),
        auto_assist_max=_int('AUTO_ASSIST_MAX', 10),
        auto_assist_prompt=_read(os.getenv('AUTO_ASSIST_PROMPT_FILE') or ''),
        bp_agent=_bool('BP_AGENT', False),
        bp_poll_interval=_int('BP_POLL_INTERVAL', 60),
        bp_decisions=frozenset(x.lower() for x in (_list('BP_AGENT_DECISIONS') or ('approve', 'reject', 'ok', 'cancel'))),
        bp_min_confidence=_float('BP_MIN_CONFIDENCE', 0.5),
        bp_escalate_to=_int('BP_AGENT_ESCALATE_TO', 0) or None,
        bp_max_per_cycle=_int('BP_MAX_PER_CYCLE', 5),
        bp_timeline_note=_bool('BP_TIMELINE_NOTE', True),
        bp_extra_prompt=_read(os.getenv('BP_AGENT_PROMPT_FILE') or ''),
        personal_agents_file=os.getenv('PERSONAL_AGENTS_FILE') or 'personal_agents.toml',
        private_chat_mode=(os.getenv('PRIVATE_CHAT_MODE') or 'personal').lower(),
        personal_memory_max=_int('PERSONAL_MEMORY_MAX', 50),
        personal_history=_int('PERSONAL_HISTORY', 40),
        tools_disabled=frozenset(_list('TOOLS_DISABLED')),
        plugins=_list('TOOL_PLUGINS'),
        plugins_dir=Path(os.getenv('PLUGINS_DIR') or 'plugins'),
        bitrix_call=(os.getenv('BITRIX_CALL') or 'read').lower(),
        bitrix_call_allow=tuple(x.lower() for x in _list('BITRIX_CALL_ALLOW')),
        bitrix_call_deny=tuple(x.lower() for x in _list('BITRIX_CALL_DENY')),
        data_dir=Path(os.getenv('DATA_DIR') or 'data'),
        log_level=(os.getenv('LOG_LEVEL') or 'INFO').upper(),
    )
