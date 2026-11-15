"""Локальное хранилище SQLite: история диалогов, привязки чатов, отслеживание таймлайнов."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
create table if not exists kv (k text primary key, v text);
create table if not exists history (
    id integer primary key autoincrement,
    conv text not null, role text not null, content text not null, ts real not null
);
create index if not exists history_conv on history(conv, id);
create table if not exists chat_binding (
    dialog_id text primary key, entity_type_id integer not null, entity_id integer not null,
    created_by integer, ts real not null
);
create table if not exists timeline_watch (
    entity_type_id integer not null, entity_id integer not null,
    last_comment_id integer not null default 0, added_by integer, ts real not null,
    primary key (entity_type_id, entity_id)
);
create table if not exists processed (key text primary key, ts real not null);
"""


class Store:
    def __init__(self, data_dir: Path):
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(data_dir / 'agent.sqlite3', check_same_thread=False)
        self._db.executescript(_SCHEMA)
        self._lock = asyncio.Lock()

    async def _run(self, sql: str, args: tuple = (), fetch: str = '') -> Any:
        async with self._lock:
            def work() -> Any:
                cur = self._db.execute(sql, args)
                self._db.commit()
                if fetch == 'one':
                    return cur.fetchone()
                if fetch == 'all':
                    return cur.fetchall()
                return cur.rowcount
            return await asyncio.to_thread(work)

    # --- kv ---
    async def get(self, key: str, default: Any = None) -> Any:
        row = await self._run('select v from kv where k=?', (key,), 'one')
        return json.loads(row[0]) if row else default

    async def set(self, key: str, value: Any) -> None:
        await self._run('insert or replace into kv(k, v) values (?, ?)', (key, json.dumps(value)))

    # --- история ---
    async def history(self, conv: str, limit: int) -> list[dict]:
        rows = await self._run(
            'select role, content from history where conv=? order by id desc limit ?',
            (conv, limit), 'all',
        )
        return [{'role': r, 'content': c} for r, c in reversed(rows)]

    async def add_history(self, conv: str, role: str, content: str) -> None:
        await self._run(
            'insert into history(conv, role, content, ts) values (?, ?, ?, ?)',
            (conv, role, content, time.time()),
        )

    async def clear_history(self, conv: str) -> None:
        await self._run('delete from history where conv=?', (conv,))

    # --- привязка чатов к объектам ---
    async def bind_chat(self, dialog_id: str, etid: int, item_id: int, user_id: int | None) -> None:
        await self._run(
            'insert or replace into chat_binding values (?, ?, ?, ?, ?)',
            (dialog_id, etid, item_id, user_id, time.time()),
        )

    async def unbind_chat(self, dialog_id: str) -> None:
        await self._run('delete from chat_binding where dialog_id=?', (dialog_id,))

    async def chat_binding(self, dialog_id: str) -> tuple[int, int] | None:
        row = await self._run(
            'select entity_type_id, entity_id from chat_binding where dialog_id=?', (dialog_id,), 'one',
        )
        return (row[0], row[1]) if row else None

    async def chats_for_entity(self, etid: int, item_id: int) -> list[str]:
        rows = await self._run(
            'select dialog_id from chat_binding where entity_type_id=? and entity_id=?', (etid, item_id), 'all',
        )
        return [r[0] for r in rows]

    # --- отслеживание таймлайна ---
    async def watch(self, etid: int, item_id: int, last_id: int, user_id: int | None) -> None:
        await self._run(
            'insert or replace into timeline_watch values (?, ?, ?, ?, ?)',
            (etid, item_id, last_id, user_id, time.time()),
        )

    async def unwatch(self, etid: int, item_id: int) -> None:
        await self._run('delete from timeline_watch where entity_type_id=? and entity_id=?', (etid, item_id))

    async def is_watched(self, etid: int, item_id: int) -> bool:
        row = await self._run(
            'select 1 from timeline_watch where entity_type_id=? and entity_id=?', (etid, item_id), 'one',
        )
        return row is not None

    async def watches(self) -> list[tuple[int, int, int]]:
        return await self._run('select entity_type_id, entity_id, last_comment_id from timeline_watch', (), 'all')

    async def set_watch_cursor(self, etid: int, item_id: int, last_id: int) -> None:
        await self._run(
            'update timeline_watch set last_comment_id=? where entity_type_id=? and entity_id=? and last_comment_id<?',
            (last_id, etid, item_id, last_id),
        )

    # --- защита от повторной обработки событий ---
    async def mark_processed(self, key: str) -> bool:
        """True, если ключ новый (событие нужно обработать)."""
        try:
            await self._run('insert into processed(key, ts) values (?, ?)', (key, time.time()))
        except sqlite3.IntegrityError:
            return False
        if hash(key) % 200 == 0:
            await self._run('delete from processed where ts < ?', (time.time() - 7 * 86400,))
        return True
