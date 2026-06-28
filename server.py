"""Bitrix24 MCP Server — лёгкий сервер для Hermes
===============================================
v2: CRM + IM + imbot. Два вебхука, бот ID=18. Таймаут 60с.
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# Загружаем .env из папки сервера
load_dotenv(Path(__file__).parent / ".env")

# ============================================================
#  Конфигурация
# ============================================================

def get_webhook_url() -> str:
    url = os.getenv("BITRIX24_WEBHOOK_URL", "")
    if not url:
        raise RuntimeError(
            "BITRIX24_WEBHOOK_URL не задан.\n"
            "Создайте .env с BITRIX24_WEBHOOK_URL=https://your.bitrix24.ru/rest/1/token/",
        )
    return url.rstrip("/")


def _crm_request(method: str, params: dict | None = None) -> dict:
    """CRM API: сделки, контакты, таймлайн."""
    resp = requests.post(f"{CRM_WH}/{method}", json=params or {}, timeout=60)
    resp.raise_for_status()
    return resp.json()

def _im_request(method: str, params: dict | None = None) -> dict:
    """IM API: чаты, сообщения."""
    resp = requests.post(f"{IM_WH}/{method}", json=params or {}, timeout=60)
    resp.raise_for_status()
    return resp.json()

def _bot_request(method: str, params: dict | None = None) -> dict:
    """Imbot API: отправка сообщений ботом."""
    bot_params = {"botId": BOT_ID, "botToken": os.getenv("BITRIX24_BOT_TOKEN", "")}


def _safe_json(data) -> str:
    """Безопасная сериализация в JSON."""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


# ============================================================
#  Инструменты: Лиды (CRM Lead)
# ============================================================

def create_lead(
    title: str,
    name: str = "",
    phone: str = "",
    email: str = "",
    source_description: str = "",
    comments: str = "",
    amount: float = 0,
) -> str:
    """Создать лид в Bitrix24 CRM.

    Args:
        title: Название лида (обязательно)
        name: Имя контакта
        phone: Телефон
        email: Email
        source_description: Источник заявки (сайт, звонок, чат...)
        comments: Комментарий
        amount: Сумма (ориентировочная)

    Returns:
        JSON с ID созданного лида

    """
    fields = {"TITLE": title}
    if name:
        fields["NAME"] = name
    if phone:
        fields["PHONE"] = [{"VALUE": phone, "VALUE_TYPE": "WORK"}]
    if email:
        fields["EMAIL"] = [{"VALUE": email, "VALUE_TYPE": "WORK"}]
    if source_description:
        fields["SOURCE_DESCRIPTION"] = source_description
    if comments:
        fields["COMMENTS"] = comments
    if amount:
        fields["OPPORTUNITY"] = amount

    result = _crm_request("crm.lead.add", {"fields": fields})
    return _safe_json(result)


def list_leads(
    search: str = "",
    stage_id: str = "",
    limit: int = 10,
) -> str:
    """Получить список лидов с возможностью поиска и фильтрации.

    Args:
        search: Поисковая строка (ищет по названию, имени, телефону)
        stage_id: ID стадии (если нужны лиды на конкретной стадии)
        limit: Число результатов (макс 50)

    Returns:
        JSON со списком лидов

    """
    filt = {}
    if search:
        filt["SEARCH_CONTENT"] = search
    if stage_id:
        filt["STAGE_ID"] = stage_id

    result = _crm_request("crm.lead.list", {
        "filter": filt,
        "select": ["ID", "TITLE", "NAME", "PHONE", "EMAIL", "STATUS_ID", "OPPORTUNITY", "DATE_CREATE"],
        "order": {"DATE_CREATE": "DESC"},
    })
    return _safe_json(result)


def get_lead(lead_id: int) -> str:
    """Получить лид по ID.

    Args:
        lead_id: ID лида

    Returns:
        JSON с данными лида

    """
    result = _crm_request("crm.lead.get", {"id": lead_id})
    return _safe_json(result)


# ============================================================
#  Инструменты: Сделки (CRM Deal)
# ============================================================

def create_deal(
    title: str,
    opportunity: float = 0,
    contact_name: str = "",
    contact_phone: str = "",
    contact_email: str = "",
    stage_id: str = "",
    comments: str = "",
) -> str:
    """Создать сделку в Bitrix24 CRM.

    Args:
        title: Название сделки (обязательно)
        opportunity: Сумма сделки
        contact_name: Имя контакта
        contact_phone: Телефон
        contact_email: Email
        stage_id: ID стадии (если не указана — первая стадия)
        comments: Комментарий

    Returns:
        JSON с ID созданной сделки

    """
    fields = {
        "TITLE": title,
        "OPPORTUNITY": opportunity,
    }
    if comments:
        fields["COMMENTS"] = comments
    if stage_id:
        fields["STAGE_ID"] = stage_id

    # Если есть контакт — создаём его и привязываем
    if contact_name or contact_phone or contact_email:
        contact_fields = {}
        if contact_name:
            contact_fields["NAME"] = contact_name
        if contact_phone:
            contact_fields["PHONE"] = [{"VALUE": contact_phone, "VALUE_TYPE": "WORK"}]
        if contact_email:
            contact_fields["EMAIL"] = [{"VALUE": contact_email, "VALUE_TYPE": "WORK"}]
        contact_result = _crm_request("crm.contact.add", {"fields": contact_fields})
        if contact_result.get("result"):
            fields["CONTACT_ID"] = contact_result["result"]

    result = _crm_request("crm.deal.add", {"fields": fields})
    return _safe_json(result)


def list_deals(
    search: str = "",
    stage_id: str = "",
    contact_id: int = 0,
    limit: int = 10,
) -> str:
    """Получить список сделок с фильтрацией.

    Args:
        search: Поисковая строка
        stage_id: ID стадии воронки
        contact_id: ID контакта (показать только сделки этого контакта)
        limit: Число результатов

    Returns:
        JSON со списком сделок

    """
    filt = {}
    if search:
        filt["SEARCH_CONTENT"] = search
    if stage_id:
        filt["STAGE_ID"] = stage_id
    if contact_id:
        filt["CONTACT_ID"] = contact_id

    result = _crm_request("crm.deal.list", {
        "filter": filt,
        "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CONTACT_ID", "DATE_CREATE", "DATE_MODIFY"],
        "order": {"DATE_CREATE": "DESC"},
    })
    return _safe_json(result)


def get_deal(deal_id: int) -> str:
    """Получить сделку по ID.

    Args:
        deal_id: ID сделки

    Returns:
        JSON с данными сделки

    """
    result = _crm_request("crm.deal.get", {"id": deal_id})
    return _safe_json(result)


def update_deal_stage(deal_id: int, stage_id: str) -> str:
    """Обновить стадию сделки.

    Args:
        deal_id: ID сделки
        stage_id: ID новой стадии (например: 'C14:PREPARATION', 'C14:WON')

    Returns:
        JSON с результатом

    """
    result = _crm_request("crm.deal.update", {
        "id": deal_id,
        "fields": {"STAGE_ID": stage_id},
    })
    return _safe_json(result)


# ============================================================
#  Инструменты: Контакты (CRM Contact)
# ============================================================

def search_contacts(
    query: str,
    limit: int = 10,
) -> str:
    """Найти контакты по имени, телефону или email.

    Args:
        query: Строка поиска
        limit: Число результатов

    Returns:
        JSON со списком контактов

    """
    result = _crm_request("crm.contact.list", {
        "filter": {"SEARCH_CONTENT": query},
        "select": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL"],
        "order": {"ID": "DESC"},
    })
    return _safe_json(result)


def get_contact(contact_id: int) -> str:
    """Получить контакт по ID.

    Args:
        contact_id: ID контакта

    Returns:
        JSON с данными контакта

    """
    result = _crm_request("crm.contact.get", {"id": contact_id})
    return _safe_json(result)


# ============================================================
#  Инструменты: Задачи / Активности (CRM Activity)
# ============================================================

def create_activity(
    subject: str,
    description: str = "",
    owner_type_id: int = 2,  # 2 = сделка
    owner_id: int = 0,
    deadline: str = "",
    priority: int = 2,  # 0=низкий, 1=средний, 2=высокий
) -> str:
    """Создать дело/задачу в CRM (звонок, встреча, задача).

    Args:
        subject: Тема дела
        description: Описание
        owner_type_id: Тип владельца (1=лид, 2=сделка, 3=контакт)
        owner_id: ID владельца
        deadline: Дедлайн в формате 'DD.MM.YYYY HH:MM:SS'
        priority: Приоритет (0=низкий, 1=средний, 2=высокий)

    Returns:
        JSON с ID дела

    """
    fields = {
        "SUBJECT": subject,
        "DESCRIPTION": description,
        "OWNER_TYPE_ID": owner_type_id,
        "OWNER_ID": owner_id,
        "PRIORITY": priority,
    }
    if deadline:
        fields["DEADLINE"] = deadline
    else:
        import datetime
        fields["DEADLINE"] = (datetime.datetime.now() + datetime.timedelta(days=1)).strftime("%d.%m.%Y %H:%M:%S")

    result = _crm_request("crm.activity.add", {"fields": fields})
    return _safe_json(result)


# ============================================================
#  Инструменты: Универсальные / Утилиты
# ============================================================

def get_deal_stages() -> str:
    """Получить список всех стадий воронки сделок.

    Returns:
        JSON со стадиями: ID, название, категория

    """
    # Используем crm.dealcategory.list для получения воронок и стадий
    categories = _crm_request("crm.dealcategory.list")
    result = {"categories": []}
    if "result" in categories:
        for cat in categories["result"]:
            stages_result = _crm_request("crm.dealcategory.stage.list", {"id": cat.get("ID", 0)})
            result["categories"].append({
                "id": cat.get("ID"),
                "name": cat.get("NAME"),
                "stages": stages_result.get("result", []),
            })
    return _safe_json(result)


def call_batch(commands: str) -> str:
    """Выполнить пакет запросов к Bitrix24 API (batch).

    Args:
        commands: JSON-строка с командами. Формат:
            {"cmd1": "crm.deal.list?filter[STAGE_ID]=C14:PREPARATION", "cmd2": "crm.lead.list"}

    Returns:
        JSON с результатами всех команд

    """
    import json as _json
    cmds = _json.loads(commands) if isinstance(commands, str) else commands

    # Bitrix24 batch принимает команды через 'cmd'
    params = {}
    for key, cmd in cmds.items():
        params[key] = cmd if isinstance(cmd, str) else _json.dumps(cmd)

    result = _crm_request("batch", params)
    return _safe_json(result)


# ============================================================
#  MCP Server
# ============================================================

def main():
    """Точка входа для MCP-сервера через stdio."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("Bitrix24 CRM Server")

    # Регистрируем все инструменты
    tools = [
        create_lead, list_leads, get_lead,
        create_deal, list_deals, get_deal, update_deal_stage,
        search_contacts, get_contact,
        create_activity,
        get_deal_stages, call_batch,
    ]
    for tool in tools:
        mcp.tool()(tool)

    # Запускаем
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

