"""Обработка событий Битрикс24: сообщения боту, добавление в чат, комментарии таймлайна."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

from b24agent.agent import Agent
from b24agent.bitrix import Bitrix, BitrixError
from b24agent.config import Settings
from b24agent.entities import EntityRegistry
from b24agent.formatting import md_to_bb, mentions, strip_bb
from b24agent.imbot import ImBot
from b24agent.store import Store
from b24agent.tools import BotContext, Toolbox, ToolContext

log = logging.getLogger(__name__)

HELP = """Я ИИ-агент портала. Что я умею:
• искать и показывать сделки, контакты, компании, лиды и элементы смарт-процессов;
• создавать и менять объекты CRM, переводить по стадиям (если разрешено);
• работать с задачами: найти мои задачи, создать задачу, прокомментировать;
• работать в контексте объекта: «привяжи этот чат к сделке 123», «создай чат по сделке 123»;
• отвечать в таймлайне: упомяните меня в комментарии объекта или скажите «следи за таймлайном сделки 123».
Команды: /help — справка, /clear — забыть историю этого диалога, /context — к какому объекту привязан чат."""


class BotService:
    def __init__(self, b24: Bitrix, settings: Settings, store: Store, entities: EntityRegistry,
                 toolbox: Toolbox, agent: Agent, imbot: ImBot):
        self.b24 = b24
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
        """Поставить событие в обработку (HTTP-ответ Битриксу отдаётся сразу)."""
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
        if name == 'ONIMBOTMESSAGEADD':
            await self.on_message(data)
        elif name == 'ONIMBOTJOINCHAT':
            await self.on_join(data)
        elif name == 'ONIMBOTDELETE':
            log.warning('Бот удалён из портала')
            await self.store.set('bot_id', None)
            self.imbot.bot_id = None
        elif name in {'ONCRMTIMELINECOMMENTADD', 'ONCRMTIMELINECOMMENTUPDATE'}:
            fields = data.get('FIELDS') or {}
            if fields.get('ID'):
                await self.on_timeline_comment(int(fields['ID']), updated=name.endswith('UPDATE'))
        else:
            log.debug('Событие %s не обрабатывается', name)

    def _ours(self, data: dict) -> bool:
        bots = data.get('BOT') or {}
        ids = set()
        for key, val in bots.items():
            if str(key).isdigit():
                ids.add(int(key))
            if isinstance(val, dict) and str(val.get('BOT_ID', '')).isdigit():
                ids.add(int(val['BOT_ID']))
        return not ids or self.imbot.bot_id in ids

    def _allowed(self, user_id: int | None) -> bool:
        return not self.s.allowed_users or (user_id in self.s.allowed_users)

    # ---------------------------------------------------------------- chat
    async def _binding(self, dialog_id: str, params: dict) -> tuple[int, int] | None:
        bound = await self.store.chat_binding(dialog_id)
        if bound:
            return bound
        if str(params.get('CHAT_ENTITY_TYPE', '')).upper() in {'CRM', 'B24AGENT'}:
            parsed = await self.entities.from_chat_entity(str(params.get('CHAT_ENTITY_ID', '')))
            if parsed:
                return parsed[0].id, parsed[1]
        return None

    async def on_message(self, data: dict) -> None:
        if not self._ours(data):
            return
        params = data.get('PARAMS') or {}
        user = data.get('USER') or {}
        if str(user.get('IS_BOT', 'N')).upper() == 'Y':
            return
        dialog_id = str(params.get('DIALOG_ID') or '')
        user_id = _int(params.get('FROM_USER_ID')) or _int(user.get('ID'))
        msg_id = params.get('MESSAGE_ID')
        if not dialog_id or (msg_id and not await self.store.mark_processed(f'msg:{msg_id}')):
            return
        text = strip_bb(str(params.get('MESSAGE') or ''), self.imbot.bot_id)
        if not text and params.get('FILES'):
            text = '(пользователь прислал файл без текста)'
        if not text:
            return
        if not self._allowed(user_id):
            await self.imbot.send(dialog_id, 'У вас нет доступа к ИИ-агенту. Обратитесь к администратору портала.')
            return

        conv = f'im:{dialog_id}'
        cmd = text.strip().lower()
        if cmd in {'/help', 'помощь', '/start'}:
            await self.imbot.send(dialog_id, HELP)
            return
        if cmd in {'/clear', '/reset', 'сброс'}:
            await self.store.clear_history(conv)
            await self.imbot.send(dialog_id, 'История диалога очищена.')
            return

        bound = await self._binding(dialog_id, params)
        if cmd == '/context':
            if bound:
                ctx = self._ctx(user_id, dialog_id, conv, bound)
                await self.imbot.send(dialog_id, md_to_bb(await self.toolbox.entity_summary(ctx, *bound)))
            else:
                await self.imbot.send(dialog_id, 'Чат не привязан к объекту CRM.')
            return

        chat_type = str(params.get('CHAT_TYPE') or ('P' if dialog_id.isdigit() else 'C')).upper()
        sender = str(user.get('NAME') or '') or await self.toolbox.user_name(self._ctx(user_id, None, conv), user_id)
        async with self._locks[conv]:
            await self.imbot.typing(dialog_id)
            ctx = self._ctx(user_id, dialog_id, conv, bound)
            lines = ['Канал: ' + ('личный чат с пользователем' if chat_type == 'P' else 'групповой чат')
                     + f' (DIALOG_ID {dialog_id}).']
            if bound:
                lines.append('Чат привязан к объекту — по умолчанию все вопросы о нём: '
                             + await self.toolbox.entity_summary(ctx, *bound))
            user_text = text if chat_type == 'P' else f'{sender} (ID {user_id}): {text}'
            answer = await self._ask(ctx, conv, user_text, lines)
            await self.imbot.send(dialog_id, md_to_bb(answer))

    async def on_join(self, data: dict) -> None:
        if not self._ours(data):
            return
        params = data.get('PARAMS') or {}
        dialog_id = str(params.get('DIALOG_ID') or '')
        if not dialog_id:
            return
        chat_type = str(params.get('CHAT_TYPE') or '').upper()
        if chat_type == 'P' or dialog_id.isdigit():
            await self.imbot.send(dialog_id, HELP)
            return
        bound = await self._binding(dialog_id, params)
        text = (f'Всем привет! Я {self.s.bot_name}. В групповом чате обращайтесь ко мне через упоминание '
                f'(@{self.s.bot_name}).')
        if bound:
            et = await self.entities.get(bound[0])
            text += f' Этот чат связан с объектом «{et.title}» #{bound[1]} — я работаю в его контексте.'
        else:
            text += ' Чтобы я работал в контексте объекта CRM, напишите: «привяжи чат к сделке 123».'
        await self.imbot.send(dialog_id, text)

    # ---------------------------------------------------------------- timeline
    async def on_timeline_comment(self, comment_id: int, comment: dict | None = None, updated: bool = False) -> None:
        if updated:
            return  # реагируем только на новые комментарии
        bot_id = self.imbot.bot_id
        if comment is None:
            try:
                comment = await self.b24.call('crm.timeline.comment.get', {'id': comment_id})
            except BitrixError as exc:
                log.warning('Комментарий %s: %s', comment_id, exc)
                return
        comment = comment or {}
        author = _int(comment.get('AUTHOR_ID'))
        raw = str(comment.get('COMMENT') or '')
        if not raw or (bot_id and author == bot_id):
            return
        try:
            et = await self.entities.from_timeline_type(str(comment.get('ENTITY_TYPE', '')))
        except ValueError:
            return
        item_id = _int(comment.get('ENTITY_ID'))
        if not item_id:
            return
        watched = await self.store.is_watched(et.id, item_id)
        triggered = mentions(raw, bot_id) or (watched and self.s.timeline_reply_mode != 'mention')
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
            answer = await self._ask(ctx, conv, f'{name} (ID {author}): {strip_bb(raw, bot_id)}', lines)
            text = (f'[USER={author}]{name}[/USER], ' if author else '') + md_to_bb(answer)
            fields: dict[str, Any] = {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type, 'COMMENT': text}
            if bot_id:
                fields['AUTHOR_ID'] = bot_id
            new_id = await self.b24.call('crm.timeline.comment.add', {'fields': fields})
            if new_id and watched:
                await self.store.set_watch_cursor(et.id, item_id, int(new_id))

    async def poll_timelines(self) -> None:
        """Запасной режим: опрос таймлайнов отслеживаемых объектов (если события не доставляются)."""
        interval = self.s.timeline_poll_interval
        log.info('Опрос таймлайнов отслеживаемых объектов каждые %s с', interval)
        while True:
            await asyncio.sleep(interval)
            for etid, item_id, last_id in await self.store.watches():
                try:
                    et = await self.entities.get(etid)
                    rows, _ = await self.b24.list('crm.timeline.comment.list', {
                        'filter': {'ENTITY_ID': item_id, 'ENTITY_TYPE': et.timeline_type},
                        'select': ['ID', 'ENTITY_ID', 'ENTITY_TYPE', 'AUTHOR_ID', 'COMMENT', 'CREATED'],
                        'order': {'ID': 'DESC'}}, limit=20)
                    for row in sorted((r for r in rows if int(r['ID']) > last_id), key=lambda r: int(r['ID'])):
                        row.setdefault('ENTITY_TYPE', et.timeline_type)
                        row.setdefault('ENTITY_ID', item_id)
                        await self.on_timeline_comment(int(row['ID']), row)
                except Exception:
                    log.exception('Опрос таймлайна %s/%s', etid, item_id)

    # ---------------------------------------------------------------- common
    def _ctx(self, user_id: int | None, dialog_id: str | None, conv: str,
             bound: tuple[int, int] | None = None) -> ToolContext:
        return ToolContext(
            b24=self.b24, entities=self.entities, settings=self.s, store=self.store, user_id=user_id,
            bot=BotContext(imbot=self.imbot, dialog_id=dialog_id, conv=conv, bound=bound),
        )

    async def _ask(self, ctx: ToolContext, conv: str, user_text: str, lines: list[str]) -> str:
        history = await self.store.history(conv, self.s.history_limit)
        try:
            answer = await self.agent.run(ctx, history, user_text, lines)
        except Exception as exc:
            log.exception('Ошибка агента')
            return f'Не получилось обработать запрос: {type(exc).__name__}. Попробуйте ещё раз позже.'
        answer = answer.strip() or 'Готово.'
        await self.store.add_history(conv, 'user', user_text)
        await self.store.add_history(conv, 'assistant', answer)
        return answer


def _int(v: Any) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
