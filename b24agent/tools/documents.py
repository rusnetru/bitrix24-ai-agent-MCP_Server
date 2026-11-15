"""Переписка и файлы объектов CRM: письма из таймлайна, вложения, файлы на Диске, чтение содержимого.

Ссылка на файл (``ref``) — устойчивый идентификатор, который возвращают crm_files/crm_email_read
и принимает file_read: ``disk:<id>``, ``attached:<id>``, ``crm:<entityTypeId>:<itemId>:<field>:<fileId>``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from b24agent.bitrix import BitrixError
from b24agent.files import FileError, download, extract_text, html_to_text, resolve
from b24agent.tools.base import (
    ENTITY,
    ITEM_ID,
    LIMIT,
    Toolbox,
    ToolContext,
    arg_int,
    clean,
    schema,
)

log = logging.getLogger(__name__)

EMAIL_PROVIDER = 'CRM_EMAIL'
EMAIL_TYPE_ID = 4
DIRECTION = {'1': 'входящее', '2': 'исходящее'}
ACT_SELECT = ['ID', 'SUBJECT', 'DIRECTION', 'CREATED', 'START_TIME', 'PROVIDER_ID', 'TYPE_ID', 'AUTHOR_ID',
              'RESPONSIBLE_ID', 'COMPLETED', 'DESCRIPTION', 'DESCRIPTION_TYPE', 'SETTINGS', 'COMMUNICATIONS', 'FILES']


def activity_text(act: dict) -> str:
    raw = str(act.get('DESCRIPTION') or '')
    if str(act.get('DESCRIPTION_TYPE')) == '3' or re.search(r'<(p|div|br|html|table)\b', raw, re.I):
        return html_to_text(raw)
    return re.sub(r'\[/?[a-z]+[^\]]*\]', '', raw).strip()  # bbcode → текст


def _addresses(act: dict) -> dict:
    meta = ((act.get('SETTINGS') or {}) if isinstance(act.get('SETTINGS'), dict) else {}).get('EMAIL_META') or {}
    out = {k: meta.get(k) for k in ('from', 'to', 'cc') if meta.get(k)}
    comms = [c.get('VALUE') for c in act.get('COMMUNICATIONS') or [] if isinstance(c, dict) and c.get('VALUE')]
    if comms:
        out.setdefault('client' if str(act.get('DIRECTION')) == '2' else 'from', ', '.join(comms))
    return out


def activity_file_refs(act: dict) -> list[str]:
    refs = []
    for f in act.get('FILES') or []:
        fid = f.get('id') if isinstance(f, dict) else f
        if fid:
            refs.append(f'disk:{fid}')
    return refs


def is_email(act: dict) -> bool:
    return str(act.get('PROVIDER_ID')) == EMAIL_PROVIDER or str(act.get('TYPE_ID')) == str(EMAIL_TYPE_ID)


async def list_emails(ctx: ToolContext, etid: int, item_id: int, limit: int, select: list[str] | None = None) -> list:
    rows, _ = await ctx.b24.list('crm.activity.list', {
        'filter': {'OWNER_TYPE_ID': etid, 'OWNER_ID': item_id, 'TYPE_ID': EMAIL_TYPE_ID},
        'select': select or ACT_SELECT, 'order': {'ID': 'DESC'}}, limit=limit)
    return [r for r in rows if is_email(r)]


async def _file_name(ctx: ToolContext, ref: str) -> dict:
    try:
        meta = await resolve(ctx.b24, ref)
        return {'ref': ref, 'name': meta.name, 'size': meta.size}
    except (BitrixError, FileError, ValueError) as exc:
        return {'ref': ref, 'name': '?', 'error': str(exc)[:200]}


# ---------------------------------------------------------------- инструменты
async def _emails(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    limit = min(arg_int(args, 'limit', 20) or 20, 100)
    rows = await list_emails(ctx, et.id, int(args['id']), limit)
    out = []
    for r in rows:
        text = activity_text(r)
        out.append(clean({
            'id': r.get('ID'), 'subject': r.get('SUBJECT'), 'direction': DIRECTION.get(str(r.get('DIRECTION'))),
            'date': r.get('START_TIME') or r.get('CREATED'), **_addresses(r),
            'files': len(r.get('FILES') or []) or None, 'preview': text[:300] + ('…' if len(text) > 300 else ''),
        }))
    return {'entity': et.title, 'id': int(args['id']), 'count': len(out), 'emails': out,
            'hint': 'Полный текст и вложения — crm_email_read(id)'}


async def _email_read(ctx: ToolContext, args: dict) -> Any:
    act = await ctx.b24.call('crm.activity.get', {'id': int(args['id'])})
    if not act:
        return {'error': 'Письмо не найдено'}
    text = activity_text(act)
    max_chars = min(arg_int(args, 'max_chars', 15000) or 15000, 60000)
    files = [await _file_name(ctx, ref) for ref in activity_file_refs(act)[:30]]
    return clean({
        'id': act.get('ID'), 'subject': act.get('SUBJECT'), 'direction': DIRECTION.get(str(act.get('DIRECTION'))),
        'date': act.get('START_TIME') or act.get('CREATED'), **_addresses(act),
        'owner': {'entityTypeId': act.get('OWNER_TYPE_ID'), 'id': act.get('OWNER_ID')},
        'body': text[:max_chars] + ('… [обрезано]' if len(text) > max_chars else ''),
        'attachments': files, 'hint': 'Содержимое вложения — file_read(ref)' if files else None,
    })


async def _files(ctx: ToolContext, args: dict) -> Any:
    et = await ctx.entities.get(args['entity'])
    item_id = int(args['id'])
    found: list[dict] = []
    # 1) файловые поля элемента
    item = (await ctx.b24.call('crm.item.get', {'entityTypeId': et.id, 'id': item_id}))['item']
    meta = await ctx.tb.fields(ctx, et)
    for key, m in meta.items():
        ftype = str(m.get('type') or '')
        if ftype not in {'file', 'disk_file'} or not item.get(key):
            continue
        values = item[key] if isinstance(item[key], list) else [item[key]]
        for v in values:
            if ftype == 'file' and isinstance(v, dict) and v.get('id'):
                found.append({'source': f'поле «{m.get("title") or key}»',
                              'ref': f'crm:{et.id}:{item_id}:{key}:{v["id"]}', 'name': v.get('name')})
            elif ftype == 'disk_file' and str(v).lstrip('n').isdigit():
                found.append({'source': f'поле «{m.get("title") or key}»', 'ref': f'attached:{str(v).lstrip("n")}'})
    # 2) вложения комментариев таймлайна
    comments, _ = await ctx.b24.list('crm.timeline.comment.list', {
        'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type},
        'select': ['ID', 'CREATED', 'AUTHOR_ID', 'FILES'], 'order': {'ID': 'DESC'}}, limit=200)
    for c in comments:
        files = c.get('FILES') or {}
        for fid, f in (files.items() if isinstance(files, dict) else enumerate(files)):
            f = f if isinstance(f, dict) else {}
            found.append({'source': f'комментарий #{c.get("ID")} от {c.get("CREATED", "")[:10]}',
                          'ref': f'disk:{f.get("id") or fid}', 'name': f.get('name'), 'size': f.get('size')})
    # 3) вложения дел (письма, встречи, звонки)
    acts, _ = await ctx.b24.list('crm.activity.list', {
        'filter': {'OWNER_TYPE_ID': et.id, 'OWNER_ID': item_id},
        'select': ['ID', 'SUBJECT', 'PROVIDER_ID', 'TYPE_ID', 'CREATED', 'FILES'], 'order': {'ID': 'DESC'}}, limit=200)
    for a in acts:
        kind = 'письмо' if is_email(a) else 'дело'
        for ref in activity_file_refs(a):
            found.append({'source': f'{kind} #{a.get("ID")} «{(a.get("SUBJECT") or "")[:60]}»', 'ref': ref})
    # имена для тех, где их нет
    seen = set()
    out = []
    for f in found:
        if f['ref'] in seen:
            continue
        seen.add(f['ref'])
        if not f.get('name'):
            f.update({k: v for k, v in (await _file_name(ctx, f['ref'])).items() if k != 'ref'})
        out.append(clean(f))
        if len(out) >= 100:
            break
    return {'entity': et.title, 'id': item_id, 'count': len(out), 'files': out,
            'hint': 'Прочитать файл — file_read(ref)'}


async def read_file_text(ctx: ToolContext, ref: str) -> tuple[str, str]:
    """(имя, текст) файла с кэшированием в SQLite по ref + размеру."""
    meta = await resolve(ctx.b24, ref)
    cached = await ctx.store.file_text(f'{ref}|{meta.size}')
    if cached:
        return cached
    data = await download(meta, ctx.settings.portal_url, ctx.settings.verify_ssl, ctx.settings.files_max_mb << 20)
    text = extract_text(meta.name, data)
    await ctx.store.save_file_text(f'{ref}|{meta.size}', meta.name, meta.size, text)
    return meta.name, text


async def _file_read(ctx: ToolContext, args: dict) -> Any:
    ref = str(args.get('ref') or '').strip()
    if not ref and args.get('disk_file_id'):
        ref = f'disk:{int(args["disk_file_id"])}'
    if not ref:
        return {'error': 'Укажите ref (из crm_files / crm_email_read) или disk_file_id'}
    try:
        name, text = await read_file_text(ctx, ref)
    except FileError as exc:
        return {'error': str(exc)}
    offset = max(arg_int(args, 'offset', 0) or 0, 0)
    size = min(arg_int(args, 'max_chars', ctx.settings.file_read_chars) or ctx.settings.file_read_chars, 60000)
    chunk = text[offset:offset + size]
    return clean({'name': name, 'total_chars': len(text), 'offset': offset, 'text': chunk,
                  'next_offset': offset + size if offset + size < len(text) else None})


def register(tb: Toolbox) -> None:
    tb.add('crm_emails', 'Письма (e-mail) в таймлайне элемента CRM/смарт-процесса: тема, направление, дата, '
           'адресаты, начало текста, число вложений. Вызывай, когда спрашивают о переписке с клиентом.',
           schema({'entity': ENTITY, 'id': ITEM_ID, 'limit': LIMIT}, ['entity', 'id']), _emails)
    tb.add('crm_email_read', 'Полный текст письма из таймлайна (по id из crm_emails) и список его вложений с ref.',
           schema({'id': {'type': 'integer'}, 'max_chars': {'type': 'integer'}}, ['id']), _email_read)
    tb.add('crm_files', 'Все файлы элемента CRM/смарт-процесса: файловые поля, вложения комментариев таймлайна, '
           'писем и дел (хранятся на Диске). Возвращает ref для file_read.',
           schema({'entity': ENTITY, 'id': ITEM_ID}, ['entity', 'id']), _files)
    tb.add('file_read', 'Прочитать содержимое файла как текст: PDF, DOCX, XLSX, PPTX, ODT, RTF, TXT, CSV, HTML, EML. '
           'ref — из crm_files/crm_email_read (или disk_file_id). Длинные файлы читаются частями: '
           'передай next_offset в offset.',
           schema({'ref': {'type': 'string'}, 'disk_file_id': {'type': 'integer'}, 'offset': {'type': 'integer'},
                   'max_chars': {'type': 'integer'}}), _file_read)
