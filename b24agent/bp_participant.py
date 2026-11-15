"""ИИ-агент как участник бизнес-процессов.

В дизайнере БП шаг («Утверждение документа», «Ознакомление», «Запрос дополнительной информации»)
назначается пользователю, от которого создан вебхук (сервисная учётка «ИИ-агент»). Агент опрашивает
свои задания (``bizproc.task.list``), изучает документ инструментами в режиме ТОЛЬКО ЧТЕНИЯ и
выполняет задание (``bizproc.task.complete``). Текст задания из дизайнера — это инструкция для агента.

Решение принимает код по проверенному JSON-ответу модели — сама модель задание не выполняет.
Разрешённые решения ограничиваются ``BP_AGENT_DECISIONS``; всё прочее (и «escalate» от модели)
уходит человеку: бот пишет ``BP_AGENT_ESCALATE_TO`` и, если возможно, делегирует задание.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import replace
from typing import Any

from b24agent.agent import Agent, _extract_json
from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.entities import EntityRegistry
from b24agent.formatting import md_to_bb
from b24agent.imbot import ImBotBase
from b24agent.store import Store
from b24agent.tools import Toolbox, ToolContext

log = logging.getLogger(__name__)

STATUS_CODE = {'approve': 1, 'reject': 2, 'ok': 3, 'cancel': 4}
# Какие решения допустимы для типа задания
ACTIVITY_DECISIONS = {
    'ApproveActivity': {'approve', 'reject'},
    'ReviewActivity': {'ok'},
    'RequestInformationActivity': {'ok'},
    'RequestInformationOptionalActivity': {'ok', 'cancel'},
}
ACTIVITY_TITLE = {
    'ApproveActivity': 'утверждение (approve — утвердить, reject — отклонить)',
    'ReviewActivity': 'ознакомление (ok — ознакомлен)',
    'RequestInformationActivity': 'запрос информации (ok — заполнить поля fields)',
    'RequestInformationOptionalActivity': 'запрос информации с возможностью отказа '
                                          '(ok — заполнить fields, cancel — отказаться)',
}
MAX_ATTEMPTS = 3

PROMPT = """Ты — участник бизнес-процесса «{workflow}» на портале Битрикс24. Тебе назначено задание.

Задание: {name}
Тип: {activity}
Инструкция из задания (её написал автор процесса — выполняй её):
{description}

Документ процесса: {document}
{fields_block}
Как работать:
1. Изучи документ инструментами (карточка, письма, файлы, задачи, история) — столько, сколько нужно по инструкции.
2. Ничего не изменяй в CRM: у тебя режим только чтения, задание выполнит система по твоему решению.
3. Если данных недостаточно, инструкция неясна или решение рискованное — выбери "escalate".

В конце ответь ТОЛЬКО JSON-объектом, без пояснений вокруг:
{{"decision": "{decisions}|escalate", "comment": "обоснование для участников процесса (2–6 предложений)",
 "fields": {{"<Id поля>": значение}}, "confidence": 0.0-1.0}}"""


def parse_document(task: dict) -> tuple[str, str]:
    """(тип, id) документа задания: ('DEAL', '123'), ('DYNAMIC_1038', '5'), ('lists', '777')."""
    doc = task.get('DOCUMENT_ID')
    raw = ''
    module = ''
    if isinstance(doc, list) and doc:
        module = str(doc[0])
        raw = str(doc[-1])
    elif isinstance(doc, dict):
        vals = list(doc.values())
        module, raw = (str(vals[0]), str(vals[-1])) if vals else ('', '')
    elif doc:
        raw = str(doc)
    m = re.fullmatch(r'(DYNAMIC_\d+|[A-Z_]+?)_(\d+)', raw)
    if m and module in ('crm', ''):
        return m.group(1), m.group(2)
    url = str(task.get('DOCUMENT_URL') or '')
    m = re.search(r'/crm/type/(\d+)/details/(\d+)', url) or re.search(r'/crm/(deal|lead|contact|company|quote)/details/(\d+)', url)
    if m:
        kind = m.group(1)
        return (f'DYNAMIC_{kind}' if kind.isdigit() else kind.upper()), m.group(2)
    return module or 'document', raw


def task_fields(task: dict) -> list[dict]:
    params = task.get('PARAMETERS') or {}
    fields = (params.get('Fields') or params.get('FIELDS') or []) if isinstance(params, dict) else []
    out = []
    for f in fields.values() if isinstance(fields, dict) else fields:
        if not isinstance(f, dict):
            continue
        fid = f.get('Id') or f.get('id') or f.get('FieldName')
        if fid:
            out.append({'id': str(fid), 'name': f.get('Name') or f.get('name') or fid,
                        'type': str(f.get('Type') or f.get('type') or 'string').lower(),
                        'required': str(f.get('Required') or f.get('required') or '') in {'1', 'Y', 'True', 'true'},
                        'multiple': str(f.get('Multiple') or f.get('multiple') or '') in {'1', 'Y', 'True', 'true'},
                        'options': f.get('Options') or f.get('options') or None,
                        'description': f.get('Description') or f.get('description') or ''})
    return out


def coerce(field: dict, value: Any) -> Any:
    t = field['type']
    if field['multiple'] and not isinstance(value, list):
        value = [value]
    if isinstance(value, list):
        return [coerce({**field, 'multiple': False}, v) for v in value]
    if t == 'bool':
        return 'Y' if str(value).strip().lower() in {'y', 'yes', 'да', 'true', '1'} else 'N'
    if t == 'int':
        return int(float(value))
    if t == 'double':
        return float(str(value).replace(',', '.'))
    if t == 'select' and isinstance(field.get('options'), dict):
        opts = {str(k): str(v) for k, v in field['options'].items()}
        if str(value) in opts:
            return str(value)
        by_name = {v.lower(): k for k, v in opts.items()}
        if str(value).lower() in by_name:
            return by_name[str(value).lower()]
        msg = f'«{value}» нет среди вариантов поля «{field["name"]}»: {", ".join(opts.values())}'
        raise ValueError(msg)
    if t == 'user' and str(value).isdigit():
        return f'user_{value}'
    return value


class BPParticipant:
    def __init__(self, b24: Bitrix, settings: Settings, store: Store, entities: EntityRegistry,
                 toolbox: Toolbox, agent: Agent, imbot: ImBotBase | None):
        self.b24 = b24
        self.s = settings
        self.store = store
        self.entities = entities
        self.toolbox = toolbox
        self.agent = agent
        self.imbot = imbot

    async def loop(self) -> None:
        log.info('Участник БП: опрос заданий пользователя %s каждые %s с, разрешено: %s',
                 self.b24.webhook_user_id, self.s.bp_poll_interval, ', '.join(sorted(self.s.bp_decisions)))
        while True:
            try:
                await self.poll_once()
            except Exception:
                log.exception('Участник БП: ошибка опроса')
            await asyncio.sleep(self.s.bp_poll_interval)

    async def poll_once(self) -> int:
        flt: dict = {'STATUS': 0}
        if self.b24.webhook_user_id:
            flt['USER_ID'] = self.b24.webhook_user_id
        tasks, _ = await self.b24.list('bizproc.task.list', {
            'SELECT': ['ID', 'WORKFLOW_ID', 'DOCUMENT_ID', 'DOCUMENT_NAME', 'DOCUMENT_URL', 'NAME', 'DESCRIPTION',
                       'ACTIVITY', 'PARAMETERS', 'MODIFIED', 'WORKFLOW_TEMPLATE_NAME'],
            'FILTER': flt, 'ORDER': {'ID': 'ASC'}}, limit=50)
        done = 0
        for task in tasks:
            if done >= self.s.bp_max_per_cycle:
                break
            tid = str(task.get('ID'))
            if await self.store.get(f'bp_done:{tid}'):
                continue
            attempts = int(await self.store.get(f'bp_attempt:{tid}', 0) or 0)
            if attempts >= MAX_ATTEMPTS:
                continue
            await self.store.set(f'bp_attempt:{tid}', attempts + 1)
            await self.handle(task)
            done += 1
        return done

    # ---------------------------------------------------------------- одно задание
    async def handle(self, task: dict) -> None:
        activity = str(task.get('ACTIVITY') or '')
        allowed_by_type = ACTIVITY_DECISIONS.get(activity)
        if allowed_by_type is None:
            await self._escalate(task, f'тип задания {activity or "?"} агент не поддерживает', None)
            return
        fields = task_fields(task)
        doc_type, doc_id = parse_document(task)
        etid_item = await self._crm_ref(doc_type, doc_id)
        ctx = ToolContext(b24=self.b24, entities=self.entities, settings=replace(self.s, allow_write=False),
                          store=self.store, tb=self.toolbox)
        document = f'{task.get("DOCUMENT_NAME") or ""} ({doc_type} {doc_id})'
        if task.get('DOCUMENT_URL'):
            url = str(task['DOCUMENT_URL'])
            document += f', ссылка: {self.s.portal_url + url if url.startswith("/") else url}'
        lines = ['Канал: задание бизнес-процесса (ответ — строго JSON с решением).']
        if etid_item:
            lines.append('Документ: ' + await self.toolbox.entity_summary(ctx, *etid_item))
        fields_block = ''
        if fields:
            fields_block = 'Поля, которые нужно заполнить (fields):\n' + '\n'.join(
                f'- {f["id"]}: «{f["name"]}», тип {f["type"]}' + (' (обязательно)' if f['required'] else '')
                + (f', варианты: {json.dumps(f["options"], ensure_ascii=False)}' if f['options'] else '')
                + (f' — {f["description"]}' if f['description'] else '') for f in fields) + '\n'
        prompt = PROMPT.format(
            workflow=task.get('WORKFLOW_TEMPLATE_NAME') or task.get('WORKFLOW_ID') or '',
            name=task.get('NAME') or '', activity=ACTIVITY_TITLE[activity],
            description=re.sub(r'<[^>]+>', ' ', str(task.get('DESCRIPTION') or '(нет)')).strip()[:6000],
            document=document, fields_block=fields_block,
            decisions='|'.join(sorted(allowed_by_type)))
        if self.s.bp_extra_prompt:
            prompt += '\n\nПравила компании для заданий БП:\n' + self.s.bp_extra_prompt
        conv = f'bp:{task.get("WORKFLOW_ID")}'
        history = await self.store.history(conv, 10)
        decision = None
        error = ''
        for _ in range(2):  # одна повторная попытка, если ответ не разобрался
            try:
                answer = await self.agent.run(ctx, history, prompt + (f'\n\nПредыдущий ответ не принят: {error}'
                                                                     if error else ''), lines)
            except Exception as exc:
                log.exception('Участник БП: ошибка агента')
                error = f'{type(exc).__name__}'
                continue
            decision, error = self._validate(answer, allowed_by_type, fields)
            if decision:
                break
        if not decision:
            await self._escalate(task, f'не удалось получить корректное решение ({error})', None)
            return
        await self.store.add_history(conv, 'assistant', json.dumps(decision, ensure_ascii=False))
        if decision['decision'] == 'escalate':
            await self._escalate(task, decision.get('comment') or 'агент передал решение человеку', decision)
            return
        if decision['decision'] not in self.s.bp_decisions:
            await self._escalate(task, f'решение «{decision["decision"]}» агенту не разрешено принимать '
                                       f'самостоятельно (BP_AGENT_DECISIONS)', decision)
            return
        if decision.get('confidence') is not None and float(decision['confidence']) < self.s.bp_min_confidence:
            await self._escalate(task, f'низкая уверенность агента ({decision["confidence"]})', decision)
            return
        await self._complete(task, decision, etid_item)

    def _validate(self, answer: str, allowed: set[str], fields: list[dict]) -> tuple[dict | None, str]:
        data = _extract_json(answer or '')
        if not data:
            return None, 'ответ не содержит JSON'
        dec = str(data.get('decision') or '').strip().lower()
        dec = {'yes': 'approve', 'no': 'reject', 'утвердить': 'approve', 'отклонить': 'reject'}.get(dec, dec)
        if dec != 'escalate' and dec not in allowed:
            return None, f'decision должен быть одним из: {", ".join(sorted(allowed))}, escalate'
        out: dict[str, Any] = {'decision': dec, 'comment': str(data.get('comment') or '').strip(),
                               'confidence': data.get('confidence')}
        if dec == 'ok' and fields:
            given = data.get('fields') or {}
            values = {}
            try:
                for f in fields:
                    val = given.get(f['id'], given.get(f['name']))
                    if val in (None, '', []):
                        if f['required']:
                            return None, f'не заполнено обязательное поле {f["id"]} «{f["name"]}»'
                        continue
                    if f['type'] == 'file':
                        continue
                    values[f['id']] = coerce(f, val)
            except (ValueError, TypeError) as exc:
                return None, str(exc)
            out['fields'] = values
        return out, ''

    async def _complete(self, task: dict, decision: dict, etid_item: tuple[int, int] | None) -> None:
        tid = str(task['ID'])
        comment = f'ИИ-агент: {decision["comment"]}'.strip()
        params: dict[str, Any] = {'TASK_ID': int(tid), 'STATUS': STATUS_CODE[decision['decision']], 'COMMENT': comment}
        if decision.get('fields'):
            params['FIELDS'] = decision['fields']
        try:
            await self.b24.call('bizproc.task.complete', params)
        except BitrixError as exc:
            if exc.code == 'ERROR_TASK_COMPLETED':
                await self.store.set(f'bp_done:{tid}', 'already')
                return
            log.warning('Участник БП: задание %s не выполнено: %s', tid, exc)
            if exc.code in {'ERROR_TASK_TYPE', 'ERROR_TASK_VALIDATION'}:
                await self._escalate(task, f'Битрикс24 отклонил выполнение: {exc.description or exc.code}', decision)
            return
        await self.store.set(f'bp_done:{tid}', decision['decision'])
        log.info('Участник БП: задание %s «%s» → %s', tid, task.get('NAME'), decision['decision'])
        if self.s.bp_timeline_note and etid_item:
            await self._timeline(etid_item, f'[b]ИИ-агент[/b] выполнил задание бизнес-процесса «{task.get("NAME")}»: '
                                            f'[b]{decision["decision"]}[/b]\n{md_to_bb(decision["comment"])}')

    async def _escalate(self, task: dict, reason: str, decision: dict | None) -> None:
        tid = str(task['ID'])
        await self.store.set(f'bp_done:{tid}', 'escalated')
        log.info('Участник БП: задание %s передано человеку: %s', tid, reason)
        text = (f'Задание бизнес-процесса «{task.get("NAME")}» по документу «{task.get("DOCUMENT_NAME")}» '
                f'требует решения человека.\nПричина: {reason}')
        if decision and decision.get('comment'):
            text += f'\nМнение агента ({decision.get("decision")}): {decision["comment"]}'
        url = str(task.get('DOCUMENT_URL') or '')
        if url:
            text += f'\nДокумент: {self.s.portal_url + url if url.startswith("/") else url}'
        to = self.s.bp_escalate_to
        if to:
            try:
                await self.b24.call('bizproc.task.delegate', {
                    'TASK_IDS': [int(tid)], 'FROM_USER_ID': self.b24.webhook_user_id, 'TO_USER_ID': to})
                text += '\nЗадание делегировано вам.'
            except BitrixError as exc:
                log.info('bizproc.task.delegate: %s', exc)
                text += '\nДелегировать автоматически не удалось — откройте задание у пользователя «ИИ-агент» ' \
                        'или делегируйте его вручную.'
            if self.imbot and self.imbot.bot_id:
                try:
                    await self.imbot.send(str(to), text)
                except BitrixError as exc:
                    log.warning('Не удалось уведомить %s: %s', to, exc)

    async def _crm_ref(self, doc_type: str, doc_id: str) -> tuple[int, int] | None:
        if not doc_id.isdigit():
            return None
        try:
            et = await self.entities.get(doc_type)
        except ValueError:
            return None
        return et.id, int(doc_id)

    async def _timeline(self, ref: tuple[int, int], text: str) -> None:
        et = await self.entities.get(ref[0])
        try:
            await self.b24.call('crm.timeline.comment.add', {'fields': {
                'ENTITY_ID': ref[1], 'ENTITY_TYPE': et.timeline_type, 'COMMENT': text}})
        except BitrixError as exc:
            log.warning('Комментарий в таймлайн: %s', exc)
