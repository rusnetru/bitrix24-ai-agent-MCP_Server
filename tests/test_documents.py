import io
import json
import zipfile
from dataclasses import replace

import pytest

from b24agent import files as files_mod
from b24agent.files import FileMeta, extract_text, html_to_text
from b24agent.tools import ToolContext
from test_agent import BOT_ID, FakeBitrix, make_bot  # noqa: F401  (фикстуры env/settings ниже)
from test_agent import env, settings  # noqa: F401


def _zip(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def test_extract_docx_xlsx_pptx():
    docx = _zip({'word/document.xml': f'<w:document {W}><w:body><w:p><w:r><w:t>Договор поставки</w:t></w:r></w:p>'
                                      '<w:p><w:r><w:t>Сумма 100 000 ₽</w:t></w:r></w:p></w:body></w:document>'})
    text = extract_text('dogovor.docx', docx)
    assert 'Договор поставки' in text and 'Сумма 100 000' in text and text.index('Договор') < text.index('Сумма')

    s = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    xlsx = _zip({
        'xl/sharedStrings.xml': f'<sst {s}><si><t>Товар</t></si><si><t>Цена</t></si><si><t>Болт</t></si></sst>',
        'xl/workbook.xml': f'<workbook {s}><sheets><sheet name="Прайс" sheetId="1"/></sheets></workbook>',
        'xl/worksheets/sheet1.xml': f'<worksheet {s}><sheetData><row><c t="s"><v>0</v></c><c t="s"><v>1</v></c></row>'
                                    '<row><c t="s"><v>2</v></c><c><v>15.5</v></c></row></sheetData></worksheet>'})
    text = extract_text('price.xlsx', xlsx)
    assert '--- Прайс ---' in text and 'Товар | Цена' in text and 'Болт | 15.5' in text

    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    pptx = _zip({'ppt/slides/slide2.xml': f'<p:sld {a} xmlns:p="p"><a:p><a:r><a:t>Итоги</a:t></a:r></a:p></p:sld>',
                 'ppt/slides/slide1.xml': f'<p:sld {a} xmlns:p="p"><a:p><a:r><a:t>Цели</a:t></a:r></a:p></p:sld>'})
    text = extract_text('deck.pptx', pptx)
    assert text.index('Цели') < text.index('Итоги')


def test_extract_html_eml_csv_and_unsupported():
    assert html_to_text('<p>Добрый день,</p><div>счёт&nbsp;во&nbsp;вложении</div><script>x()</script>') == \
        'Добрый день,\n\nсчёт во вложении'
    eml = ('From: client@corp.ru\nTo: sales@company.ru\nSubject: =?utf-8?b?0JfQsNC/0YDQvtGB?=\n'
           'Content-Type: text/plain; charset=utf-8\n\nПришлите КП до пятницы').encode()
    text = extract_text('letter.eml', eml)
    assert 'client@corp.ru' in text and 'Запрос' in text and 'до пятницы' in text
    assert extract_text('t.csv', 'a;b\n1;2'.encode('cp1251')) == 'a | b\n1 | 2'
    assert 'OCR' in extract_text('scan.jpg', b'\xff\xd8')
    assert 'старый двоичный формат' in extract_text('old.doc', b'\xd0\xcf')


class DocsBitrix(FakeBitrix):
    def respond(self, method, params):
        if method == 'crm.item.fields':
            return {'fields': {'title': {'title': 'Название'},
                               'ufCrm5_Contract': {'title': 'Договор', 'type': 'file'},
                               'ufCrm5_Docs': {'title': 'Документы', 'type': 'disk_file'}}}
        if method == 'crm.item.get':
            return {'item': {'id': params['id'], 'title': 'Проект', 'assignedById': 3,
                             'ufCrm5_Contract': {'id': 10, 'urlMachine': 'https://b24.local/rest/getFile?auth=x'},
                             'ufCrm5_Docs': ['n77']}}
        if method == 'crm.timeline.comment.list':
            return [{'ID': '5', 'CREATED': '2026-11-15T10:00:00+03:00', 'AUTHOR_ID': '3',
                     'FILES': {'301': {'id': 301, 'name': 'ТЗ.docx', 'size': 1000}}}]
        if method == 'crm.activity.list':
            return [{'ID': '900', 'SUBJECT': 'Запрос КП', 'PROVIDER_ID': 'CRM_EMAIL', 'TYPE_ID': '4',
                     'DIRECTION': '1', 'CREATED': '2026-11-15T10:01:00+03:00',
                     'DESCRIPTION': '<p>Пришлите КП</p>', 'DESCRIPTION_TYPE': '3',
                     'COMMUNICATIONS': [{'VALUE': 'client@corp.ru'}], 'FILES': [{'id': 401, 'url': '/x'}]},
                    {'ID': '901', 'SUBJECT': 'Звонок', 'PROVIDER_ID': 'VOXIMPLANT_CALL', 'TYPE_ID': '2'}]
        if method == 'crm.activity.get':
            return {'ID': '900', 'SUBJECT': 'Запрос КП', 'DIRECTION': '1', 'DESCRIPTION': '<p>Пришлите КП</p>',
                    'DESCRIPTION_TYPE': '3', 'COMMUNICATIONS': [{'VALUE': 'client@corp.ru'}],
                    'FILES': [{'id': 401}], 'OWNER_TYPE_ID': '1038', 'OWNER_ID': '5'}
        if method == 'disk.file.get':
            return {'ID': params['id'], 'NAME': f'file{params["id"]}.txt', 'SIZE': 12,
                    'DOWNLOAD_URL': f'https://b24.local/rest/download.json?id={params["id"]}'}
        if method == 'disk.attachedObject.get':
            return {'ID': params['id'], 'OBJECT_ID': 88, 'NAME': 'Устав.pdf', 'SIZE': 5,
                    'DOWNLOAD_URL': 'https://b24.local/rest/download.json?att=77'}
        return super().respond(method, params)


@pytest.fixture
def denv(settings):  # noqa: F811
    from b24agent.entities import EntityRegistry
    from b24agent.store import Store
    from b24agent.tools import build_toolbox
    b24 = DocsBitrix()
    return b24, Store(settings.data_dir), EntityRegistry(b24), build_toolbox()


async def test_crm_files_collects_all_sources(denv, settings):  # noqa: F811
    b24, store, reg, tb = denv
    ctx = ToolContext(b24=b24, entities=reg, settings=settings, store=store, tb=tb)
    res = json.loads(await tb.execute(ctx, 'crm_files', {'entity': 'Проекты', 'id': 5}))
    refs = {f['ref']: f for f in res['files']}
    assert 'crm:1038:5:ufCrm5_Contract:10' in refs  # поле «Файл»
    assert refs['attached:77']['name'] == 'Устав.pdf'  # поле «Файл (Диск)» → ID привязки
    assert refs['disk:301']['name'] == 'ТЗ.docx'  # вложение комментария
    assert refs['disk:401']['source'].startswith('письмо #900')  # вложение письма


async def test_emails_and_email_read(denv, settings):  # noqa: F811
    b24, store, reg, tb = denv
    ctx = ToolContext(b24=b24, entities=reg, settings=settings, store=store, tb=tb)
    res = json.loads(await tb.execute(ctx, 'crm_emails', {'entity': 1038, 'id': 5}))
    assert [e['id'] for e in res['emails']] == ['900']  # звонок отфильтрован
    assert res['emails'][0]['direction'] == 'входящее' and res['emails'][0]['preview'] == 'Пришлите КП'
    mail = json.loads(await tb.execute(ctx, 'crm_email_read', {'id': 900}))
    assert mail['body'] == 'Пришлите КП' and mail['attachments'][0]['ref'] == 'disk:401'


async def test_file_read_downloads_extracts_caches_and_pages(denv, settings, monkeypatch):  # noqa: F811
    b24, store, reg, tb = denv
    calls = []

    async def fake_download(meta: FileMeta, portal, verify, max_bytes, http_timeout=120):
        calls.append(meta.url)
        return ('строка\n' * 100).encode()

    monkeypatch.setattr('b24agent.tools.documents.download', fake_download)
    ctx = ToolContext(b24=b24, entities=reg, settings=replace(settings, file_read_chars=50), store=store, tb=tb)
    r1 = json.loads(await tb.execute(ctx, 'file_read', {'ref': 'disk:301'}))
    assert r1['name'] == 'file301.txt' and r1['total_chars'] == 700 and r1['next_offset'] == 50
    r2 = json.loads(await tb.execute(ctx, 'file_read', {'ref': 'disk:301', 'offset': 50}))
    assert r2['offset'] == 50 and len(calls) == 1  # второй раз из кэша
    assert calls[0].startswith('https://b24.local/rest/download.json')


async def test_crm_file_field_uses_url_machine(denv):
    b24, *_ = denv
    meta = await files_mod.resolve(b24, 'crm:1038:5:ufCrm5_Contract:10')
    assert meta.url == 'https://b24.local/rest/getFile?auth=x'


async def test_auto_assist_new_email_posts_readonly_help(settings, monkeypatch):  # noqa: F811
    from b24agent.agent import Agent
    from b24agent.bot import BotService
    from b24agent.entities import EntityRegistry
    from b24agent.imbot import ImBotV2
    from b24agent.store import Store
    from b24agent.tools import build_toolbox

    from test_agent import FakeLLM

    b24 = DocsBitrix()

    def respond(method, params, base=b24.respond):
        if method == 'crm.item.list':
            return {'items': [{'id': 5, 'title': 'Проект', 'assignedById': 3,
                               'lastActivityTime': '2026-11-15T10:02:00+03:00'}]}
        return base(method, params)

    b24.respond = respond
    store = Store(settings.data_dir)
    await store.set('bot_id_v2', BOT_ID)
    s = replace(settings, auto_assist_types=('1038',), auto_assist_target=frozenset({'timeline', 'chat'}))
    imbot = ImBotV2(b24, s, store)
    await imbot.load()
    tb = build_toolbox()
    llm = FakeLLM([{'content': 'Клиент просит КП до пятницы.'}, {'content': 'ТЗ: три этапа.'}], s)
    bot = BotService(b24, s, store, EntityRegistry(b24), tb, Agent(llm, tb), imbot)
    await store.set('assist_cursor_1038', '2026-11-15T09:00:00+03:00')

    import asyncio as aio

    async def stop(_):
        raise aio.CancelledError

    monkeypatch.setattr('b24agent.bot.asyncio.sleep', stop)
    with pytest.raises(aio.CancelledError):
        await bot.assist_loop()
    comments = [p['fields']['COMMENT'] for m, p in b24.calls if m == 'crm.timeline.comment.add']
    assert len(comments) == 2 and all(c.startswith('[b]ИИ-помощник[/b]') for c in comments)
    dms = [p for m, p in b24.calls if m == 'imbot.v2.Chat.Message.send']
    assert {p['dialogId'] for p in dms} == {'3'}  # ответственному в личку
    tools_offered = {t['function']['name'] for t in llm.seen[0][1]}
    assert 'crm_email_read' in tools_offered and 'crm_update' not in tools_offered  # только чтение
    # повторный проход не дублирует справки
    with pytest.raises(aio.CancelledError):
        await bot.assist_loop()
    assert len([1 for m, _ in b24.calls if m == 'crm.timeline.comment.add']) == 2
