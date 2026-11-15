"""Преобразование Markdown-ответа модели в BB-коды Битрикс24 и обратно."""

from __future__ import annotations

import re
from collections.abc import Iterable

_CODE_BLOCK = re.compile(r'```[a-zA-Z0-9_+-]*\n?(.*?)```', re.DOTALL)


def md_to_bb(text: str) -> str:
    blocks: list[str] = []

    def keep(m: re.Match) -> str:
        blocks.append(m.group(1).rstrip())
        return f'\x00{len(blocks) - 1}\x00'

    text = _CODE_BLOCK.sub(keep, text)
    text = re.sub(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)', r'[url=\2]\1[/url]', text)
    text = re.sub(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*$', r'[b]\1[/b]', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'[b]\1[/b]', text)
    text = re.sub(r'__(.+?)__', r'[b]\1[/b]', text)
    text = re.sub(r'(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)', r'[i]\1[/i]', text)
    text = re.sub(r'~~(.+?)~~', r'[s]\1[/s]', text)
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    text = re.sub(r'^\s*[-*]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*---+\s*$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return re.sub(r'\x00(\d+)\x00', lambda m: f'[code]{blocks[int(m.group(1))]}[/code]', text).strip()


def strip_bb(text: str, bot_id: int | Iterable[int] | None = None) -> str:
    """Убрать упоминания агента (bot_id — один ID или несколько) и BB-разметку из входящего сообщения."""
    ids = [bot_id] if isinstance(bot_id, int) else list(bot_id or [])
    for uid in ids:
        text = re.sub(rf'\[USER={uid}\].*?\[/USER\]\s*[,:]?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[USER=(\d+)\](.*?)\[/USER\]', r'\2 (ID \1)', text, flags=re.IGNORECASE)
    text = re.sub(r'\[/?(b|i|u|s|code|quote|color[^\]]*|size[^\]]*|url[^\]]*)\]', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\[br\]', '\n', text, flags=re.IGNORECASE)
    return text.strip()


def mentions(text: str, user_id: int | None) -> bool:
    return bool(user_id) and re.search(rf'\[USER={user_id}\]', text or '', re.IGNORECASE) is not None
