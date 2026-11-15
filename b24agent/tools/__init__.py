"""Инструменты агента и MCP-сервера.

Состав:
- встроенные модули (BUILTIN_MODULES) — каждый экспортирует ``register(tb: Toolbox)``;
- плагины компании: модули из ``TOOL_PLUGINS`` (через запятую, импортируемые пути) и все ``*.py``
  из каталога ``PLUGINS_DIR`` (по умолчанию ./plugins). Плагин тоже экспортирует ``register(tb)``
  и может добавлять, переопределять (то же имя) или удалять (``tb.remove``) инструменты.

См. docs/EXTENDING.md.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path

from b24agent.config import Settings
from b24agent.tools.base import (
    ENTITY,
    ITEM_ID,
    LIMIT,
    BotContext,
    Tool,
    Toolbox,
    ToolContext,
    arg_int,
    as_dict,
    clean,
    schema,
    to_int,
)

log = logging.getLogger(__name__)

BUILTIN_MODULES = (
    'b24agent.tools.crm',
    'b24agent.tools.tasks',
    'b24agent.tools.users',
    'b24agent.tools.org',
    'b24agent.tools.processes',
    'b24agent.tools.generic',
    'b24agent.tools.chat',
)

__all__ = [
    'ENTITY', 'ITEM_ID', 'LIMIT', 'BotContext', 'Tool', 'ToolContext', 'Toolbox', 'arg_int', 'as_dict',
    'build_toolbox', 'clean', 'schema', 'to_int',
]


def _register_module(tb: Toolbox, module: object, name: str) -> None:
    register = getattr(module, 'register', None)
    if not callable(register):
        log.warning('Плагин %s не содержит функцию register(tb) — пропущен', name)
        return
    tb._group = name
    register(tb)
    tb._group = ''


def build_toolbox(settings: Settings | None = None) -> Toolbox:
    tb = Toolbox()
    for mod in BUILTIN_MODULES:
        _register_module(tb, importlib.import_module(mod), mod)
    if settings is None:
        return tb
    for mod in settings.plugins:
        _register_module(tb, importlib.import_module(mod), mod)
        log.info('Подключён плагин %s', mod)
    pdir = Path(settings.plugins_dir)
    if pdir.is_dir():
        for path in sorted(pdir.glob('*.py')):
            if path.name.startswith('_'):
                continue
            name = f'b24agent_plugin_{path.stem}'
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            _register_module(tb, module, str(path))
            log.info('Подключён плагин %s', path)
    return tb
