"""Обработка событий Битрикс24: сообщения боту, добавление в чат, комментарии таймлайна.

Источники событий (все сводятся к :meth:`BotService.handle`):
- imbot.v2 fetch — :meth:`BotService.fetch_loop` опрашивает ``imbot.v2.Event.get`` (закрытый контур);
- push на ``/bitrix/events`` — imbot.v2 webhook, imbot v1, исходящий вебхук ONCRMTIMELINECOMMENTADD;
- опрос таймлайнов — :meth:`poll_watched` (отслеживаемые объекты) и :meth:`scan_timelines`
  (недавно активные элементы выбранных типов, ловит @упоминания без событий).
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from b24agent.agent import Agent
from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.entities import EntityRegistry
from b24agent.formatting import md_to_bb, mentions, strip_bb
from b24agent.imbot import ImBotBase
from b24agent.profiles import AgentProfile, ProfileRegistry
from b24agent.store import Store
from b24agent.tools import BotContext, Toolbox, ToolContext
from b24agent.tools.documents import list_emails

log = logging.getLogger(__name__)

HELP = """Я ИИ-агент портала. Что я умею:
• искать и показывать сделки, контакты, компании, лиды и элементы смарт-процессов;
• создавать и менять объекты CRM, переводить по стадиям (если разрешено);
• работать с задачами, календарём, согласованиями бизнес-процессов, универсальными списками;
• работать в контексте объекта: «привяжи этот чат к сделке 123», «создай чат по сделке 123»;
• отвечать в таймлайне: упомяните меня в комментарии объекта или скажите «следи за таймлайном сделки 123».
Команды: /help — справка, /clear — забыть историю этого диалога, /context — к какому объекту привязан чат,
/whoami — с чьими правами я работаю, /memory — что я о вас помню (личный ассистент), /forget — забыть всё."""


DEFAULT_ASSIST = """Подготовь помощь ответственному сотруднику:
1. Суть: о чём письмо или документ (3–5 пунктов).
2. Что от нас хотят: запросы, сроки, суммы, условия.
3. Риски и на что обратить внимание (сверь с данными объекта CRM, если нужно).
4. Предлагаемые следующие шаги.
5. Для входящего письма — черновик ответа.
Ничего не изменяй в CRM — только подготовь справку. Пиши кратко и по делу."""


@dataclass
class Incoming:
    """Входящее сообщение боту, независимо от версии API."""

    dialog_id: str
    user_id: int | None
    user_name: str
    text: str
    message_id: str
    private: bool
    entity_type: str = ''  # тип сущности чата: CRM, B24AGENT, TASKS_TASK ...
    entity_id: str = ''  # DEAL|123, DYNAMIC_1038|5 ...
    is_bot: bool = False
    has_files: bool = False


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _truthy(v: Any) -> bool:
    return str(v).strip().upper() in {'Y', '1', 'TRUE'}


def parse_v1(data: dict) -> Incoming | None:
    params = data.get('PARAMS') or {}
    user = data.get('USER') or {}
    dialog_id = str(params.get('DIALOG_ID') or '')
    if not dialog_id:
        return None
    chat_type = str(params.get('CHAT_TYPE') or params.get('MESSAGE_TYPE') or '').upper()
    return Incoming(
        dialog_id=dialog_id,
        user_id=_int(params.get('FROM_USER_ID')) or _int(user.get('ID')),
        user_name=str(user.get('NAME') or ''),
        text=str(params.get('MESSAGE') or ''),
        message_id=str(params.get('MESSAGE_ID') or ''),
        private=chat_type == 'P' or (not chat_type and dialog_id.isdigit()),
        entity_type=str(params.get('CHAT_ENTITY_TYPE') or ''),
        entity_id=str(params.get('CHAT_ENTITY_ID') or ''),
        is_bot=_truthy(user.get('IS_BOT', 'N')),
        has_files=bool(params.get('FILES')),
    )


def parse_v2(data: dict) -> Incoming | None:
    msg = data.get('message') or {}
    chat = data.get('chat') or {}
    user = data.get('user') or {}
    user_id = _int(user.get('id')) or _int(msg.get('authorId'))
    private = str(chat.get('type') or '').lower() == 'private'
    dialog_id = str(user_id) if private and user_id else str(chat.get('dialogId') or '')
    if not dialog_id and chat.get('id'):
        dialog_id = f'chat{chat["id"]}'
    if not dialog_id:
        return None
    params = msg.get('params') or {}
    name = str(user.get('name') or ' '.join(filter(None, [user.get('firstName'), user.get('lastName')])))
    return Incoming(
        dialog_id=dialog_id, user_id=user_id, user_name=name, text=str(msg.get('text') or ''),
        message_id=str(msg.get('id') or ''), private=private,
        entity_type=str(chat.get('entityType') or ''), entity_id=str(chat.get('entityId') or ''),
        is_bot=_truthy(user.get('bot', False)) or _truthy(msg.get('isSystem', False)),
        has_files=isinstance(params, dict) and bool(params.get('FILE_ID')),
    )


class BotService:
    def __init__(self, b24: Bitrix, settings: Settings, store: Store, entities: EntityRegistry,
                 toolbox: Toolbox, agent: Agent, imbot: ImBotBase,
                 profiles: ProfileRegistry | None = None, fixed_profile: AgentProfile | None = None):
        self.b24 = b24
        self.profiles = profiles or ProfileRegistry(service=AgentProfile(
            key='service', kind='service', b24=b24, settings=settings, name=settings.bot_name))
        self.fixed_profile = fixed_profile  # личный бот: всегда работает как этот профиль
        self.s = settings
        self.store = store
        self.entities = entities
        self.toolbox = toolbox
        self.agent = agent
        self.imbot = imbot
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: set[asyncio.Task] = set()

    # ---------------------------------------------------------------- dispatch
    def submit(self, event: dict) -> None:
        """Поставить событие в обработку (источник события не ждёт ответа агента)."""
        task = asyncio.create_task(self._safe_handle(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _safe_handle(self, event: dict) -> None:
        try:
            await self.handle(event)
        except Exception:
            log.exception('Ошибка обработки события %s', event.get('event'))

    async def handle(self, event: dict) -> None:
        name = str(event.get('event', '')).upper()
        data = event.get('data') or {}
        log.debug('event %s', name)
        if name == 'ONIMBOTV2MESSAGEADD':
            if self._ours_v2(data):
                await self.on_message(parse_v2(data))
        elif name == 'ONIMBOTMESSAGEADD':
            if self._ours_v1(data):
                await self.on_message(parse_v1(data))
        elif name == 'ONIMBOTV2JOINCHAT':
            chat = data.get('chat') or {}
            await self.on_join(str(data.get('dialogId') or chat.get('dialogId') or ''),
                               str(chat.get('type') or '').lower() == 'private',
                               str(chat.get('entityType') or ''), str(chat.get('entityId') or ''))
        elif name == 'ONIMBOTJOINCHAT':
            p = data.get('PARAMS') or {}
            dialog = str(p.get('DIALOG_ID') or '')
            await self.on_join(dialog, str(p.get('CHAT_TYPE') or '').upper() == 'P' or dialog.isdigit(),
                               str(p.get('CHAT_ENTITY_TYPE') or ''), str(p.get('CHAT_ENTITY_ID') or ''))
        elif name in {'ONIMBOTDELETE', 'ONIMBOTV2DELETE'}:
            log.warning('Бот удалён из портала')
            self.imbot.bot_id = None
            await self.imbot._save()
        elif name in {'ONCRMTIMELINECOMMENTADD', 'ONCRMTIMELINECOMMENTUPDATE'}:
            fields = data.get('FIELDS') or {}
            if fields.get('ID') and not name.endswith('UPDATE'):
                await self.on_timeline_comment(int(fields['ID']))
        else:
            log.debug('Событие %s не обрабатывается', name)

    def _ours_v1(self, data: dict) -> bool:
        ids = set()
        for key, val in (data.get('BOT') or {}).items():
            if str(key).isdigit():
                ids.add(int(key))
            if isinstance(val, dict) and str(val.get('BOT_ID', '')).isdigit():
                ids.add(int(val['BOT_ID']))
        return not ids or self.imbot.bot_id in ids

    def _ours_v2(self, data: dict) -> bool:
        bot_id = _int((data.get('bot') or {}).get('id'))
        return bot_id is None or bot_id == self.imbot.bot_id

    def _allowed(self, user_id: int | None) -> bool:
        return (not self.s.allowed_users or user_id in self.s.allowed_users
                or self.profiles.for_user(user_id) is not None)

    def resolve_profile(self, msg: Incoming) -> tuple[AgentProfile | None, str]:
        """Чей агент отвечает на сообщение: (профиль, текст отказа)."""
        if self.fixed_profile is not None:
            if msg.user_id != self.fixed_profile.user_id:
                return None, f'Это личный ассистент другого сотрудника. Обратитесь к «{self.s.bot_name}».'
            return self.fixed_profile, ''
        if not msg.private:
            return self.profiles.service, ''  # групповые чаты и чаты объектов — универсальный агент
        mode = self.s.private_chat_mode
        personal = self.profiles.for_user(msg.user_id)
        if mode == 'service':
            return self.profiles.service, ''
        if personal:
            return personal, ''
        if mode == 'personal_only':
            return None, ('Для вас ещё не настроен личный ассистент, работающий с вашими правами. '
                          'Обратитесь к администратору портала.')
        return self.profiles.service, ''

    def mention_ids(self) -> set[int]:
        """Кого считать «упоминанием агента» в таймлайне: бот, пользователь вебхука, TIMELINE_MENTION_IDS."""
        ids = set(self.s.timeline_mention_ids)
        if self.imbot.bot_id:
            ids.add(self.imbot.bot_id)
        if self.b24.webhook_user_id:
            ids.add(self.b24.webhook_user_id)
        return ids

    # ---------------------------------------------------------------- источники событий
    async def fetch_loop(self) -> None:
        """imbot.v2 fetch: забирать события бота без входящего доступа к агенту."""
        offset_key = f'v2_event_offset{getattr(self.imbot, "key", "")}'
        offset = int(await self.store.get(offset_key, 0) or 0)
        log.info('imbot.v2 fetch: опрос событий бота %s каждые %s с', self.imbot.bot_id, self.s.bot_poll_interval)
        while True:
            try:
                res = await self.imbot.fetch(offset)
            except BitrixError as exc:
                log.warning('imbot.v2.Event.get: %s', exc)
                await asyncio.sleep(15)
                continue
            except Exception:
                log.exception('imbot.v2.Event.get')
                await asyncio.sleep(15)
                continue
            for ev in res.get('events') or []:
                self.submit({'event': ev.get('type'), 'data': ev.get('data') or {}})
            nxt = res.get('nextOffset')
            if nxt and int(nxt) != offset:
                offset = int(nxt)  # подтверждение получения произойдёт следующим вызовом с этим offset
                await self.store.set(offset_key, offset)
            await asyncio.sleep(0.5 if res.get('hasMore') else self.s.bot_poll_interval)

    async def poll_watched(self) -> None:
        """Опрос таймлайнов объектов, включённых через timeline_watch."""
        interval = self.s.timeline_poll_interval
        log.info('Опрос таймлайнов отслеживаемых объектов каждые %s с', interval)
        while True:
            await asyncio.sleep(interval)
            for etid, item_id, last_id in await self.store.watches():
                try:
                    await self._check_comments(etid, item_id, after_id=last_id)
                except Exception:
                    log.exception('Опрос таймлайна %s/%s', etid, item_id)

    async def scan_timelines(self) -> None:
        """Сканирование недавно активных элементов (lastActivityTime) выбранных типов.

        crm.timeline.comment.list фильтрует только по конкретному элементу, а события через входящий
        вебхук недоступны, поэтому находим элементы с новой активностью и проверяем их комментарии.
        """
        interval = self.s.timeline_scan_interval
        types = [await self.entities.get(t) for t in self.s.timeline_scan_types]
        log.info('Сканирование таймлайнов: %s, каждые %s с', ', '.join(t.title for t in types), interval)
        while True:
            for et in types:
                key = f'scan_cursor_{et.id}'
                try:
                    cursor = await self.store.get(key)
                    if not cursor:
                        cursor = await self.b24.call('server.time')
                        await self.store.set(key, cursor)
                        continue
                    items, _ = await self.b24.list('crm.item.list', {
                        'entityTypeId': et.id, 'filter': {'>=lastActivityTime': cursor},
                        'select': ['id', 'lastActivityTime'], 'order': {'lastActivityTime': 'ASC'}},
                        limit=200, key='items')
                    newest = cursor
                    for it in items:
                        await self._check_comments(et.id, int(it['id']), created_since=cursor)
                        newest = max(newest, str(it.get('lastActivityTime') or newest), key=_ts)
                    if newest != cursor:
                        await self.store.set(key, newest)
                except Exception:
                    log.exception('Сканирование таймлайна %s', et.title)
            await asyncio.sleep(interval)

    async def assist_loop(self) -> None:
        """Автопомощь: по новым входящим письмам и файлам у недавно активных элементов готовить справку."""
        interval = self.s.auto_assist_interval
        types = [await self.entities.get(t) for t in self.s.auto_assist_types]
        log.info('Автопомощь (%s → %s): %s, каждые %s с', ', '.join(sorted(self.s.auto_assist_on)),
                 ', '.join(sorted(self.s.auto_assist_target)), ', '.join(t.title for t in types), interval)
        while True:
            budget = self.s.auto_assist_max
            for et in types:
                key = f'assist_cursor_{et.id}'
                try:
                    cursor = await self.store.get(key)
                    if not cursor:
                        await self.store.set(key, await self.b24.call('server.time'))
                        continue
                    items, _ = await self.b24.list('crm.item.list', {
                        'entityTypeId': et.id, 'filter': {'>=lastActivityTime': cursor},
                        'select': ['id', 'title', 'assignedById', 'lastActivityTime'],
                        'order': {'lastActivityTime': 'ASC'}}, limit=200, key='items')
                    newest = cursor
                    for it in items:
                        if budget <= 0:
                            break
                        budget -= await self._assist_item(et, it, cursor)
                        newest = max(newest, str(it.get('lastActivityTime') or newest), key=_ts)
                    if newest != cursor:
                        await self.store.set(key, newest)
                except Exception:
                    log.exception('Автопомощь: %s', et.title)
            await asyncio.sleep(interval)

    async def _assist_item(self, et: Any, item: dict, since: str) -> int:
        item_id = int(item['id'])
        since_ts = _ts(since)
        jobs: list[tuple[str, str]] = []
        ctx = self._ctx(None, None, f'tl:{et.id}:{item_id}')
        if 'email' in self.s.auto_assist_on:
            for e in await list_emails(ctx, et.id, item_id, 10, select=[
                    'ID', 'SUBJECT', 'DIRECTION', 'CREATED', 'PROVIDER_ID', 'TYPE_ID']):
                if str(e.get('DIRECTION')) == '1' and _ts(e.get('CREATED')) >= since_ts:
                    jobs.append((f'assist:email:{e["ID"]}',
                                 f'По объекту пришло новое входящее письмо «{e.get("SUBJECT") or ""}» '
                                 f'(id {e["ID"]}). Прочитай его (crm_email_read) и важные вложения (file_read).'))
        if 'file' in self.s.auto_assist_on:
            rows, _ = await self.b24.list('crm.timeline.comment.list', {
                'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type},
                'select': ['ID', 'CREATED', 'AUTHOR_ID', 'FILES'], 'order': {'ID': 'DESC'}}, limit=20)
            for c in rows:
                if _ts(c.get('CREATED')) < since_ts or _int(c.get('AUTHOR_ID')) == self.b24.webhook_user_id:
                    continue
                files = c.get('FILES') or {}
                for fid, f in (files.items() if isinstance(files, dict) else enumerate(files)):
                    f = f if isinstance(f, dict) else {}
                    fid = f.get('id') or fid
                    jobs.append((f'assist:file:{fid}',
                                 f'В таймлайн объекта добавлен файл «{f.get("name") or fid}» (ref disk:{fid}). '
                                 'Прочитай его через file_read.'))
        done = 0
        for key, task in jobs:
            if await self.store.mark_processed(key):
                await self._assist(et, item, task)
                done += 1
        return done

    async def _assist(self, et: Any, item: dict, task: str) -> None:
        item_id = int(item['id'])
        conv = f'tl:{et.id}:{item_id}'
        ctx = ToolContext(b24=self.b24, entities=self.entities, settings=replace(self.s, allow_write=False),
                          store=self.store, tb=self.toolbox)
        lines = ['Канал: автоматическая справка для ответственного сотрудника (никто не спрашивал напрямую).',
                 'Объект: ' + await self.toolbox.entity_summary(ctx, et.id, item_id)]
        try:
            answer = await self.agent.run(ctx, [], f'{task}\n\n{self.s.auto_assist_prompt or DEFAULT_ASSIST}', lines)
        except Exception:
            log.exception('Автопомощь: ошибка агента')
            return
        answer = answer.strip()
        if not answer:
            return
        await self.store.add_history(conv, 'assistant', answer)  # чтобы вопросы в таймлайне видели справку
        body = md_to_bb(answer)
        if 'timeline' in self.s.auto_assist_target:
            await self.b24.call('crm.timeline.comment.add', {'fields': {
                'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type, 'COMMENT': f'[b]ИИ-помощник[/b]\n{body}'}})
        assigned = _int(item.get('assignedById'))
        if 'chat' in self.s.auto_assist_target and assigned and self.imbot.bot_id:
            url = et.url(self.s.portal_url, item_id)
            await self.imbot.send(str(assigned), f'По объекту [url={url}]{et.title} «{item.get("title") or item_id}»'
                                                 f'[/url]:\n{body}')

    async def _check_comments(self, etid: int, item_id: int, after_id: int = 0, created_since: str = '') -> None:
        et = await self.entities.get(etid)
        rows, _ = await self.b24.list('crm.timeline.comment.list', {
            'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type},
            'select': ['ID', 'ENTITY_ID', 'ENTITY_TYPE', 'AUTHOR_ID', 'COMMENT', 'CREATED'],
            'order': {'ID': 'DESC'}}, limit=20)
        since = _ts(created_since) if created_since else None
        fresh = [r for r in rows if int(r['ID']) > after_id and (since is None or _ts(r.get('CREATED')) >= since)]
        for row in sorted(fresh, key=lambda r: int(r['ID'])):
            row.setdefault('ENTITY_TYPE', et.timeline_type)
            row.setdefault('ENTITY_ID', item_id)
            await self.on_timeline_comment(int(row['ID']), row)

    # ---------------------------------------------------------------- chat
    async def _binding(self, msg: Incoming) -> tuple[int, int] | None:
        bound = await self.store.chat_binding(msg.dialog_id)
        if bound:
            return bound
        if msg.entity_type.upper() in {'CRM', 'B24AGENT'}:
            parsed = await self.entities.from_chat_entity(msg.entity_id)
            if parsed:
                return parsed[0].id, parsed[1]
        return None

    async def on_message(self, msg: Incoming | None) -> None:
        if msg is None or msg.is_bot or (self.imbot.bot_id and msg.user_id == self.imbot.bot_id):
            return
        if msg.message_id and not await self.store.mark_processed(f'msg:{msg.message_id}'):
            return
        dialog_id = msg.dialog_id
        text = strip_bb(msg.text, self.imbot.bot_id)
        if not text and msg.has_files:
            text = '(пользователь прислал файл без текста)'
        if not text:
            return
        if not self._allowed(msg.user_id):
            await self.imbot.send(dialog_id, 'У вас нет доступа к ИИ-агенту. Обратитесь к администратору портала.')
            return

        profile, refusal = self.resolve_profile(msg)
        if profile is None:
            await self.imbot.send(dialog_id, refusal)
            return
        conv = f'pa:{profile.user_id}' if profile.personal else f'im:{dialog_id}'
        cmd = text.strip().lower()
        if cmd == '/whoami':
            await self.imbot.send(dialog_id, self._whoami(profile))
            return
        if cmd in {'/memory', '/память'}:
            await self.imbot.send(dialog_id, await self._memory_text(profile))
            return
        if cmd in {'/forget', '/забудь'} and profile.personal:
            n = await self.store.forget_memory(profile.user_id)
            await self.imbot.send(dialog_id, f'Готово, личная память очищена ({n} заметок).')
            return
        if cmd in {'/help', 'помощь', '/start'}:
            await self.imbot.send(dialog_id, HELP)
            return
        if cmd in {'/clear', '/reset', 'сброс'}:
            await self.store.clear_history(conv)
            await self.imbot.send(dialog_id, 'История диалога очищена.')
            return

        bound = await self._binding(msg)
        if cmd == '/context':
            if bound:
                ctx = self._ctx(msg.user_id, dialog_id, conv, bound, profile)
                await self.imbot.send(dialog_id, md_to_bb(await self.toolbox.entity_summary(ctx, *bound)))
            else:
                await self.imbot.send(dialog_id, 'Чат не привязан к объекту CRM.')
            return

        sender = msg.user_name or await self.toolbox.user_name(self._ctx(msg.user_id, None, conv), msg.user_id)
        async with self._locks[conv]:
            await self.imbot.typing(dialog_id)
            ctx = self._ctx(msg.user_id, dialog_id, conv, bound, profile)
            lines = ['Канал: ' + ('личный чат с пользователем' if msg.private else 'групповой чат')
                     + f' (DIALOG_ID {dialog_id}).']
            if bound:
                lines.append('Чат привязан к объекту — по умолчанию все вопросы о нём: '
                             + await self.toolbox.entity_summary(ctx, *bound))
            user_text = text if msg.private else f'{sender} (ID {msg.user_id}): {text}'
            answer = await self._ask(ctx, conv, user_text, lines)
            await self.imbot.send(dialog_id, md_to_bb(answer))

    async def on_join(self, dialog_id: str, private: bool, entity_type: str, entity_id: str) -> None:
        if not dialog_id:
            return
        if private:
            await self.imbot.send(dialog_id, HELP)
            return
        bound = await self._binding(Incoming(dialog_id, None, '', '', '', False, entity_type, entity_id))
        text = (f'Всем привет! Я {self.s.bot_name}. В групповом чате обращайтесь ко мне через упоминание '
                f'(@{self.s.bot_name}).')
        if bound:
            et = await self.entities.get(bound[0])
            text += f' Этот чат связан с объектом «{et.title}» #{bound[1]} — я работаю в его контексте.'
        else:
            text += ' Чтобы я работал в контексте объекта CRM, напишите: «привяжи чат к сделке 123».'
        await self.imbot.send(dialog_id, text)

    # ---------------------------------------------------------------- timeline
    async def on_timeline_comment(self, comment_id: int, comment: dict | None = None) -> None:
        if comment is None:
            try:
                comment = await self.b24.call('crm.timeline.comment.get', {'id': comment_id})
            except BitrixError as exc:
                log.warning('Комментарий %s: %s', comment_id, exc)
                return
        comment = comment or {}
        author = _int(comment.get('AUTHOR_ID'))
        raw = str(comment.get('COMMENT') or '')
        # Комментарии пишутся от пользователя вебхука — свои ответы не обрабатываем
        if not raw or author in {self.imbot.bot_id, self.b24.webhook_user_id} - {None}:
            return
        try:
            et = await self.entities.from_timeline_type(str(comment.get('ENTITY_TYPE', '')))
        except ValueError:
            return
        item_id = _int(comment.get('ENTITY_ID'))
        if not item_id:
            return
        watched = await self.store.is_watched(et.id, item_id)
        mentioned = any(mentions(raw, uid) for uid in self.mention_ids())
        triggered = mentioned or (watched and self.s.timeline_reply_mode != 'mention')
        if watched:
            await self.store.set_watch_cursor(et.id, item_id, comment_id)
        if not triggered or not await self.store.mark_processed(f'tl:{comment_id}'):
            return
        if not self._allowed(author):
            return

        conv = f'tl:{et.id}:{item_id}'
        async with self._locks[conv]:
            ctx = self._ctx(author, None, conv, (et.id, item_id))
            name = await self.toolbox.user_name(ctx, author)
            lines = [
                'Канал: комментарии таймлайна. Твой ответ будет опубликован комментарием в таймлайне этого объекта.',
                'Объект: ' + await self.toolbox.entity_summary(ctx, et.id, item_id),
            ]
            clean_text = strip_bb(raw, self.mention_ids())
            answer = await self._ask(ctx, conv, f'{name} (ID {author}): {clean_text}', lines)
            text = (f'[USER={author}]{name}[/USER], ' if author else '') + md_to_bb(answer)
            fields: dict[str, Any] = {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type, 'COMMENT': text}
            new_id = await self.b24.call('crm.timeline.comment.add', {'fields': fields})
            if new_id and watched:
                await self.store.set_watch_cursor(et.id, item_id, int(new_id))

    # ---------------------------------------------------------------- common
    def _whoami(self, p: AgentProfile) -> str:
        if p.personal:
            mem = 'веду' if p.memory else 'не веду'
            return (f'Я {p.name} — ваш личный ассистент. Работаю с ВАШИМИ правами в Битрикс24: вижу только то, '
                    f'что видите вы, и действую от вашего имени. Личную память о вас {mem} (/memory).')
        return (f'Я {self.s.bot_name} — универсальный агент. Работаю со своими правами (сервисная учётка), '
                'а не с вашими; мои действия записываются от имени агента.')

    async def _memory_text(self, p: AgentProfile) -> str:
        if not p.personal or not p.memory:
            return 'Личная память есть только у личного ассистента.'
        notes = await self.store.memories(p.user_id)
        if not notes:
            return 'Я пока ничего о вас не запомнил. Расскажите, как вам удобно работать, — я запомню.'
        return 'Что я о вас помню:\n' + '\n'.join(f'[{i}] {f"{t}: " if t else ""}{x}' for i, t, x in notes) + \
            '\n\nЗабыть заметку: «забудь [номер]», всё — /forget.'

    def _ctx(self, user_id: int | None, dialog_id: str | None, conv: str,
             bound: tuple[int, int] | None = None, profile: AgentProfile | None = None) -> ToolContext:
        p = profile or self.profiles.service
        return ToolContext(
            b24=p.b24, entities=self.entities, settings=p.settings, store=self.store, tb=self.toolbox,
            user_id=user_id, bot=BotContext(imbot=self.imbot, dialog_id=dialog_id, conv=conv, bound=bound),
            profile=p,
        )

    async def _ask(self, ctx: ToolContext, conv: str, user_text: str, lines: list[str]) -> str:
        limit = self.s.personal_history if ctx.personal else self.s.history_limit
        history = await self.store.history(conv, limit)
        try:
            answer = await self.agent.run(ctx, history, user_text, lines)
        except Exception as exc:
            log.exception('Ошибка агента')
            return f'Не получилось обработать запрос: {type(exc).__name__}. Попробуйте ещё раз позже.'
        answer = answer.strip() or 'Готово.'
        await self.store.add_history(conv, 'user', user_text)
        await self.store.add_history(conv, 'assistant', answer)
        return answer


def _ts(value: Any) -> float:
    """Время Битрикс24 (ISO 8601 или «2024-08-29 10:30:11») → timestamp; нераспознанное → 0."""
    if not value:
        return 0.0
    text = str(value).strip().replace(' ', 'T', 1)
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return 0.0
