import json
from dataclasses import replace

from b24agent.agent import Agent
from b24agent.bot import BotService
from b24agent.entities import EntityRegistry
from b24agent.imbot import ImBotV2
from b24agent.profiles import AgentProfile, ProfileRegistry, load_profiles, verify
from b24agent.store import Store
from b24agent.tools import ToolContext, build_toolbox
from test_agent import BOT_ID, FakeBitrix, FakeLLM
from test_agent import settings  # noqa: F401  (фикстура)


class UserBitrix(FakeBitrix):
    """Вебхук конкретного сотрудника: профиль и данные «с его правами»."""

    def __init__(self, uid):
        super().__init__()
        self.webhook_user_id = uid

    def respond(self, method, params):
        if method == 'profile':
            return {'ID': str(self.webhook_user_id), 'NAME': 'Анна'}
        return super().respond(method, params)


def make_service(settings, script, personal=None, **over):  # noqa: F811
    s = replace(settings, **over)
    service_b24 = FakeBitrix()
    store = Store(s.data_dir)
    reg = ProfileRegistry(service=AgentProfile('service', 'service', service_b24, s, name='ИИ-агент'))
    if personal:
        reg.personal[personal.user_id] = personal
    tb = build_toolbox()
    llm = FakeLLM(script, s)
    imbot = ImBotV2(service_b24, s, store)
    imbot.bot_id = BOT_ID
    return BotService(service_b24, s, store, EntityRegistry(service_b24), tb, Agent(llm, tb), imbot,
                      profiles=reg), service_b24, llm


def personal_profile(settings, uid=3, **kw):  # noqa: F811
    return AgentProfile(key=f'user:{uid}', kind='personal', b24=UserBitrix(uid), user_id=uid,
                        settings=replace(settings, extra_prompt='Обращайся на «ты»'), name='Ассистент Анны',
                        memory=True, **kw)


def v2_msg(uid, text, private=True, mid=1):
    chat = {'id': 9, 'dialogId': str(uid) if private else 'chat9', 'type': 'private' if private else 'chat'}
    return {'event': 'ONIMBOTV2MESSAGEADD', 'data': {
        'bot': {'id': BOT_ID}, 'message': {'id': mid, 'authorId': uid, 'text': text}, 'chat': chat,
        'user': {'id': uid, 'name': 'Анна'}}}


def test_load_profiles_from_toml(settings, tmp_path, monkeypatch):  # noqa: F811
    cfg = tmp_path / 'pa.toml'
    cfg.write_text('''
[[agent]]
user_id = 15
name = "Ассистент Анны"
webhook_env = "PA_15"
own_bot = true
allow_write = false
prompt = "Кратко"
tools_disabled = ["bp_start"]

[[agent]]
user_id = 16
webhook = "https://b24.local/rest/1/foreign/"
''', encoding='utf-8')
    monkeypatch.setenv('PA_15', 'https://b24.local/rest/15/secret/')
    reg = load_profiles(replace(settings, personal_agents_file=str(cfg)), FakeBitrix())
    anna = reg.personal[15]
    assert anna.ok and anna.own_bot and not anna.settings.allow_write and 'bp_start' in anna.settings.tools_disabled
    assert anna.b24.webhook_user_id == 15 and 'Кратко' in anna.settings.extra_prompt
    assert not reg.personal[16].ok and 'вебхук создан пользователем 1' in reg.personal[16].problem
    assert reg.for_user(16) is None


async def test_verify_detects_foreign_webhook(settings):  # noqa: F811
    p = personal_profile(settings)
    p.b24.webhook_user_id = 3
    p.b24.respond = lambda m, prm: {'ID': '99'} if m == 'profile' else {}
    assert not (await verify(p)).ok


async def test_private_chat_routed_to_personal_rights_and_memory(settings):  # noqa: F811
    anna = personal_profile(settings)
    bot, service_b24, llm = make_service(settings, [
        {'content': '', 'tool_calls': [{'id': 'c', 'type': 'function', 'function': {
            'name': 'memory_save', 'arguments': json.dumps({'text': 'Любит короткие ответы', 'topic': 'стиль'})}}]},
        {'content': 'Запомнила!'}], personal=anna)
    await bot.store.add_memory(3, 'клиенты', 'Ключевой клиент — Ромашка', 50)
    await bot.handle(v2_msg(3, 'отвечай короче'))
    system = llm.seen[0][0][0]['content']
    assert 'ЛИЧНЫЙ ассистент' in system and 'Ключевой клиент — Ромашка' in system and 'на «ты»' in system
    offered = {t['function']['name'] for t in llm.seen[0][1]}
    assert 'memory_save' in offered
    assert [n for _, _, n in await bot.store.memories(3)] == ['Ключевой клиент — Ромашка', 'Любит короткие ответы']
    # данные — через вебхук сотрудника, сообщения — через бота (сервисный вебхук)
    assert any(m == 'user.get' for m, _ in anna.b24.calls)
    assert not any(m == 'user.get' for m, _ in service_b24.calls)
    assert any(m == 'imbot.v2.Chat.Message.send' for m, _ in service_b24.calls)


async def test_group_chat_and_users_without_profile_use_service(settings):  # noqa: F811
    anna = personal_profile(settings)
    bot, _, llm = make_service(settings, [{'content': 'ok'}, {'content': 'ok'}], personal=anna)
    await bot.handle(v2_msg(3, 'привет всем', private=False, mid=2))
    await bot.handle(v2_msg(7, 'привет', mid=3))
    for messages, tools in llm.seen:
        assert 'универсальный ИИ-агент' in messages[0]['content']
        assert 'memory_save' not in {t['function']['name'] for t in tools}


async def test_personal_only_mode_refuses_without_profile(settings):  # noqa: F811
    bot, service_b24, llm = make_service(settings, [], private_chat_mode='personal_only')
    await bot.handle(v2_msg(7, 'привет', mid=4))
    sent = [p['fields']['message'] for m, p in service_b24.calls if m == 'imbot.v2.Chat.Message.send']
    assert 'не настроен личный ассистент' in sent[0] and not llm.seen


async def test_own_personal_bot_answers_only_owner(settings):  # noqa: F811
    anna = personal_profile(settings, own_bot=True)
    bot, service_b24, llm = make_service(settings, [{'content': 'Привет, Анна'}], personal=anna)
    bot.fixed_profile = anna
    await bot.handle(v2_msg(7, 'привет', mid=5))
    await bot.handle(v2_msg(3, 'привет', mid=6))
    sent = [p['fields']['message'] for m, p in service_b24.calls if m == 'imbot.v2.Chat.Message.send']
    assert 'личный ассистент другого сотрудника' in sent[0] and sent[1] == 'Привет, Анна'
    assert len(llm.seen) == 1


async def test_memory_commands_and_secret_guard(settings):  # noqa: F811
    anna = personal_profile(settings)
    bot, service_b24, _ = make_service(settings, [], personal=anna)
    tb = bot.toolbox
    ctx = ToolContext(b24=anna.b24, entities=bot.entities, settings=anna.settings, store=bot.store, tb=tb,
                      user_id=3, profile=anna)
    assert 'error' in json.loads(await tb.execute(ctx, 'memory_save', {'text': 'мой пароль qwerty'}))
    assert json.loads(await tb.execute(ctx, 'memory_save', {'text': 'Отчёт по пятницам'}))['ok']
    service_ctx = ToolContext(b24=service_b24, entities=bot.entities, settings=bot.s, store=bot.store, tb=tb)
    assert 'error' in json.loads(await tb.execute(service_ctx, 'memory_list', {}))  # не у универсального
    await bot.handle(v2_msg(3, '/memory', mid=7))
    await bot.handle(v2_msg(3, '/whoami', mid=8))
    await bot.handle(v2_msg(3, '/forget', mid=9))
    sent = [p['fields']['message'] for m, p in service_b24.calls if m == 'imbot.v2.Chat.Message.send']
    assert 'Отчёт по пятницам' in sent[0] and 'ВАШИМИ правами' in sent[1] and 'очищена (1' in sent[2]
    assert await bot.store.memories(3) == []
