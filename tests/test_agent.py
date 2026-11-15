import json
from dataclasses import replace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from b24agent.agent import Agent, _extract_json
from b24agent.bot import BotService
from b24agent.config import Settings
from b24agent.entities import EntityRegistry
from b24agent.formatting import md_to_bb, strip_bb
from b24agent.imbot import ImBot
from b24agent.store import Store
from b24agent.tools import ToolContext, Toolbox
from b24agent.web import create_app, unflatten

BOT_ID = 77


class FakeBitrix:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.respond(method, params or {})

    async def raw(self, method, params=None):
        return {'result': await self.call(method, params)}

    async def list(self, method, params=None, limit=50, key=None):
        res = await self.call(method, params)
        if key and isinstance(res, dict):
            res = res.get(key, [])
        return list(res or [])[:limit], len(res or [])

    def respond(self, method, params):
        if method == 'crm.type.list':
            return {'types': [{'entityTypeId': 1038, 'title': 'Проекты', 'isCategoriesEnabled': 'Y',
                               'isStagesEnabled': 'Y'}]}
        if method == 'crm.item.fields':
            return {'fields': {'id': {'title': 'ID'}, 'title': {'title': 'Название'},
                               'ufCrm5_1': {'title': 'Приоритет', 'type': 'enumeration',
                                            'items': [{'ID': '10', 'VALUE': 'Высокий'}]}}}
        if method == 'crm.status.list':
            return [{'STATUS_ID': 'DT1038_5:NEW', 'NAME': 'Новый'}]
        if method == 'crm.item.list':
            return {'items': [{'id': 5, 'title': 'Внедрение', 'stageId': 'DT1038_5:NEW', 'categoryId': 5}]}
        if method == 'crm.item.get':
            return {'item': {'id': params['id'], 'title': 'Внедрение', 'stageId': 'DT1038_5:NEW',
                             'categoryId': 5, 'ufCrm5_1': '10'}}
        if method == 'crm.timeline.comment.get':
            return {'ID': params['id'], 'ENTITY_ID': 5, 'ENTITY_TYPE': 'dynamic_1038', 'AUTHOR_ID': 3,
                    'COMMENT': f'[USER={BOT_ID}]ИИ-агент[/USER] какая стадия?'}
        if method == 'user.get':
            return [{'ID': params.get('ID'), 'NAME': 'Анна', 'LAST_NAME': 'Иванова'}]
        if method in {'imbot.message.add', 'crm.timeline.comment.add'}:
            return 1001
        return True


class FakeLLM:
    def __init__(self, script, settings):
        self.script = list(script)
        self.s = settings
        self.seen = []

    async def chat(self, messages, tools=None):
        self.seen.append((messages, tools))
        return self.script.pop(0)

    async def close(self):
        pass


@pytest.fixture
def settings(tmp_path):
    return Settings(webhook_url='https://b24.local/rest/1/x/', portal_url='https://b24.local',
                    llm_base_url='http://llm', llm_model='qwen', data_dir=tmp_path, public_url='http://agent:8090',
                    event_secret='s3')


@pytest.fixture
def env(settings):
    b24 = FakeBitrix()
    store = Store(settings.data_dir)
    return b24, store, EntityRegistry(b24), Toolbox()


def native_call(name, args):
    return {'content': '', 'tool_calls': [{'id': 'c1', 'type': 'function',
                                           'function': {'name': name, 'arguments': json.dumps(args)}}]}


def test_unflatten():
    ev = unflatten({'event': 'ONIMBOTMESSAGEADD', 'data[PARAMS][MESSAGE]': 'hi', 'data[BOT][77][BOT_ID]': '77'})
    assert ev == {'event': 'ONIMBOTMESSAGEADD', 'data': {'PARAMS': {'MESSAGE': 'hi'}, 'BOT': {'77': {'BOT_ID': '77'}}}}


def test_formatting():
    assert md_to_bb('**Итог**: [сделка](https://b24.local/crm/deal/details/1/)\n- пункт') == \
        '[b]Итог[/b]: [url=https://b24.local/crm/deal/details/1/]сделка[/url]\n• пункт'
    assert strip_bb('[USER=77]ИИ[/USER], покажи [USER=3]Анна[/USER]', 77) == 'покажи Анна (ID 3)'
    assert _extract_json('```json\n{"tool": "x", "arguments": {"a": "}"}}\n```') == {'tool': 'x', 'arguments': {'a': '}'}}


async def test_entity_resolution(env):
    b24, _, reg, _ = env
    assert (await reg.get('сделка')).id == 2
    assert (await reg.get('Проекты')).id == 1038
    assert (await reg.get('dynamic_1038')).timeline_type == 'dynamic_1038'
    assert (await reg.get(1038)).task_prefix == 'T40e'
    et, iid = await reg.from_chat_entity('DYNAMIC_1038|5')
    assert (et.id, iid) == (1038, 5)
    et, iid = await reg.from_task_binding('T40e_5')
    assert (et.id, iid) == (1038, 5)


async def test_agent_native_tool_loop(env, settings):
    b24, store, reg, tb = env
    llm = FakeLLM([native_call('crm_list', {'entity': 'Проекты', 'filter': {'%title': 'Внедр'}}),
                   {'content': '<think>…</think>Найден проект **Внедрение**'}], settings)
    agent = Agent(llm, tb)
    ctx = ToolContext(b24=b24, entities=reg, settings=settings, store=store, user_id=3)
    answer = await agent.run(ctx, [], 'найди проект', [])
    assert 'Внедрение' in answer
    assert ('crm.item.list', {'entityTypeId': 1038, 'filter': {'%title': 'Внедр'},
                              'select': tb._default_select(await reg.get(1038)), 'order': {'id': 'desc'}}) \
        in [(m, {k: v for k, v in p.items() if k != 'start'}) for m, p in b24.calls]
    tool_msg = llm.seen[1][0][-1]
    assert tool_msg['role'] == 'tool' and 'stageName' in tool_msg['content']


async def test_agent_json_mode(env, settings):
    b24, store, reg, tb = env
    s = replace(settings, llm_tool_mode='json')
    llm = FakeLLM([{'content': '{"tool": "crm_get", "arguments": {"entity": 1038, "id": 5}}'},
                   {'content': '{"answer": "Приоритет высокий"}'}], s)
    ctx = ToolContext(b24=b24, entities=reg, settings=s, store=store)
    assert await Agent(llm, tb).run(ctx, [], 'приоритет?', []) == 'Приоритет высокий'
    assert 'Высокий' in llm.seen[1][0][-1]['content']  # значение списка расшифровано


async def test_read_only_hides_write_tools(env, settings):
    *_, tb = env
    names = {t.name for t in tb.available(replace(settings, allow_write=False), with_bot=True)}
    assert 'crm_list' in names and 'crm_update' not in names and 'task_add' not in names


async def make_bot(env, settings, script):
    b24, store, reg, tb = env
    await store.set('bot_id', BOT_ID)
    imbot = ImBot(b24, settings, store)
    await imbot.load()
    return BotService(b24, settings, store, reg, tb, Agent(FakeLLM(script, settings), tb), imbot)


async def test_private_chat_message(env, settings):
    bot = await make_bot(env, settings, [{'content': 'Привет! **Готов**'}])
    event = {'event': 'ONIMBOTMESSAGEADD', 'data': {
        'BOT': {str(BOT_ID): {'BOT_ID': str(BOT_ID)}},
        'PARAMS': {'DIALOG_ID': '3', 'FROM_USER_ID': '3', 'MESSAGE': 'привет', 'MESSAGE_ID': '9', 'CHAT_TYPE': 'P'},
        'USER': {'ID': '3', 'NAME': 'Анна'}}}
    await bot.handle(event)
    await bot.handle(event)  # повтор от Битрикса не обрабатывается
    sent = [p for m, p in bot.b24.calls if m == 'imbot.message.add']
    assert len(sent) == 1
    assert sent[0]['DIALOG_ID'] == '3' and sent[0]['BOT_ID'] == BOT_ID and sent[0]['CLIENT_ID']
    assert sent[0]['MESSAGE'] == 'Привет! [b]Готов[/b]'
    assert len(await bot.store.history('im:3', 10)) == 2


async def test_crm_entity_chat_context(env, settings):
    bot = await make_bot(env, settings, [{'content': 'Стадия: Новый'}])
    await bot.handle({'event': 'ONIMBOTMESSAGEADD', 'data': {
        'PARAMS': {'DIALOG_ID': 'chat12', 'FROM_USER_ID': '3', 'MESSAGE': f'[USER={BOT_ID}]ИИ[/USER] стадия?',
                   'MESSAGE_ID': '10', 'CHAT_TYPE': 'C', 'CHAT_ENTITY_TYPE': 'CRM',
                   'CHAT_ENTITY_ID': 'DYNAMIC_1038|5'}, 'USER': {'ID': '3', 'NAME': 'Анна'}}})
    system = bot.agent.llm.seen[0][0][0]['content']
    assert 'Проекты (entityTypeId=1038) #5' in system
    assert bot.agent.llm.seen[0][0][-1]['content'] == 'Анна (ID 3): стадия?'


async def test_timeline_mention_reply(env, settings):
    bot = await make_bot(env, settings, [{'content': 'Стадия «Новый».'}])
    await bot.handle({'event': 'ONCRMTIMELINECOMMENTADD', 'data': {'FIELDS': {'ID': '501'}}})
    added = [p for m, p in bot.b24.calls if m == 'crm.timeline.comment.add']
    assert len(added) == 1
    f = added[0]['fields']
    assert f['ENTITY_TYPE'] == 'dynamic_1038' and f['ENTITY_ID'] == 5 and f['AUTHOR_ID'] == BOT_ID
    assert f['COMMENT'].startswith('[USER=3]Анна Иванова[/USER], ')


async def test_timeline_without_mention_ignored(env, settings):
    bot = await make_bot(env, settings, [])
    await bot.on_timeline_comment(502, {'ENTITY_ID': 5, 'ENTITY_TYPE': 'dynamic_1038', 'AUTHOR_ID': 3,
                                        'COMMENT': 'просто заметка'})
    assert not [m for m, _ in bot.b24.calls if m == 'crm.timeline.comment.add']


async def test_bind_chat_tool(env, settings):
    bot = await make_bot(env, settings, [])
    ctx = bot._ctx(3, 'chat40', 'im:chat40')
    res = json.loads(await bot.toolbox.execute(ctx, 'chat_bind_entity', {'entity': 'Проекты', 'id': 5}))
    assert res['ok'] and await bot.store.chat_binding('chat40') == (1038, 5)


async def test_http_endpoint_secret(env, settings):
    bot = await make_bot(env, settings, [])
    got = []
    bot.submit = got.append
    async with TestClient(TestServer(create_app(bot))) as client:
        r = await client.post('/bitrix/events', data={'event': 'ONIMBOTJOINCHAT'})
        assert r.status == 403
        r = await client.post('/bitrix/events?secret=s3', data={'event': 'ONIMBOTJOINCHAT',
                                                                'data[PARAMS][DIALOG_ID]': '3'})
        assert r.status == 200
    assert got == [{'event': 'ONIMBOTJOINCHAT', 'data': {'PARAMS': {'DIALOG_ID': '3'}}}]
