"""Скачивание файлов с портала через вебхук и извлечение текста для LLM.

Маршруты скачивания (apidocs.bitrix24.ru → «Как скачать файлы»):
- файл Диска (вложения комментариев таймлайна, писем, дел): ``disk.file.get`` → ``DOWNLOAD_URL``;
- поле «Файл (Диск)» хранит ID привязки: ``disk.attachedObject.get`` → ``DOWNLOAD_URL``;
- поле «Файл» элемента CRM: ``urlMachine`` из ``crm.item.get`` (ссылка уже содержит токен).
``urlDownload``/``DETAIL_URL`` работают только в браузерной сессии — для сервера не годятся.
Ссылки бывают одноразовыми — храним ID, ссылку получаем заново перед скачиванием.

Ссылка на файл внутри агента (``ref``): ``disk:<id>``, ``attached:<id>``,
``crm:<entityTypeId>:<itemId>:<field>:<fileId>``.
"""

from __future__ import annotations

import csv
import email
import email.policy
import html
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import PurePath
from xml.etree import ElementTree

import aiohttp

from b24agent.bitrix import Bitrix, BitrixError, make_ssl

log = logging.getLogger(__name__)

TEXT_EXT = {'.txt', '.md', '.csv', '.tsv', '.json', '.xml', '.log', '.ini', '.yaml', '.yml', '.sql'}
IMAGE_EXT = {'.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tif', '.tiff', '.webp', '.heic'}


class FileError(RuntimeError):
    pass


@dataclass
class FileMeta:
    ref: str
    name: str
    size: int | None
    url: str


# ---------------------------------------------------------------- получение ссылки и загрузка
async def resolve(b24: Bitrix, ref: str) -> FileMeta:
    kind, _, rest = ref.partition(':')
    if kind == 'disk':
        try:
            f = await b24.call('disk.file.get', {'id': int(rest)})
        except BitrixError as exc:
            if exc.code not in {'ERROR_NOT_FOUND', 'ACCESS_DENIED', ''}:
                raise
            return await resolve(b24, f'attached:{rest}')  # у дел иногда ID привязки, а не файла
        return FileMeta(ref, f.get('NAME') or f'file_{rest}', _int(f.get('SIZE')), f.get('DOWNLOAD_URL') or '')
    if kind == 'attached':
        a = await b24.call('disk.attachedObject.get', {'id': int(rest)})
        url = a.get('DOWNLOAD_URL') or ''
        if not url and a.get('OBJECT_ID'):
            return await resolve(b24, f'disk:{a["OBJECT_ID"]}')
        return FileMeta(ref, a.get('NAME') or f'file_{rest}', _int(a.get('SIZE')), url)
    if kind == 'crm':
        etid, item_id, fld, file_id = rest.split(':', 3)
        item = (await b24.call('crm.item.get', {'entityTypeId': int(etid), 'id': int(item_id)}))['item']
        values = item.get(fld)
        for v in values if isinstance(values, list) else [values]:
            if isinstance(v, dict) and str(v.get('id')) == file_id:
                name = v.get('name') or PurePath(str(v.get('url', '')).split('filename=')[-1]).name or f'file_{file_id}'
                return FileMeta(ref, name, _int(v.get('size')), v.get('urlMachine') or '')
        msg = f'Файл {file_id} не найден в поле {fld}'
        raise FileError(msg)
    msg = f'Неизвестная ссылка на файл: {ref}'
    raise FileError(msg)


async def download(meta: FileMeta, portal_url: str, verify_ssl: bool | str, max_bytes: int,
                   http_timeout: float = 120) -> bytes:
    if not meta.url:
        msg = 'Портал не вернул ссылку для скачивания (нет прав на файл?)'
        raise FileError(msg)
    if meta.size and meta.size > max_bytes:
        msg = f'Файл {meta.name} слишком большой ({meta.size // 1024 // 1024} МБ > {max_bytes // 1024 // 1024} МБ)'
        raise FileError(msg)
    url = meta.url if meta.url.startswith('http') else portal_url + meta.url
    headers = {'User-Agent': 'Mozilla/5.0 (b24agent)', 'Accept': '*/*',
               'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8', 'Referer': portal_url + '/'}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=http_timeout)) as s, \
            s.get(url, headers=headers, ssl=make_ssl(verify_ssl), allow_redirects=True) as resp:
        if resp.status >= 400:
            msg = f'HTTP {resp.status} при скачивании {meta.name}'
            raise FileError(msg)
        ctype = resp.headers.get('Content-Type', '')
        data = bytearray()
        async for chunk in resp.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > max_bytes:
                msg = f'Файл {meta.name} больше лимита {max_bytes // 1024 // 1024} МБ'
                raise FileError(msg)
    low_name = meta.name.lower()
    if 'text/html' in ctype and not low_name.endswith(('.html', '.htm')) and b'<form' in bytes(data[:20000]).lower():
        msg = 'Вместо файла пришла страница авторизации — ссылка не годится для серверной загрузки'
        raise FileError(msg)
    return bytes(data)


# ---------------------------------------------------------------- извлечение текста
class _HTMLText(HTMLParser):
    BLOCK = {'p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'table', 'blockquote'}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {'script', 'style', 'head'}:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append('\n')
        elif tag in {'td', 'th'}:
            self.parts.append(' | ')

    def handle_endtag(self, tag: str) -> None:
        if tag in {'script', 'style', 'head'} and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.parts.append('\n')

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    p = _HTMLText()
    p.feed(raw)
    text = html.unescape(''.join(p.parts))
    text = re.sub(r'[ \t\xa0]+', ' ', text)
    return re.sub(r'\n\s*\n+', '\n\n', text).strip()


def _decode(data: bytes) -> str:
    for enc in ('utf-8-sig', 'cp1251', 'koi8-r'):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode('utf-8', errors='replace')


def _xml_text(xml: bytes, tags: set[str], para_tags: set[str]) -> str:
    out: list[str] = []
    for el in ElementTree.fromstring(xml).iter():
        tag = el.tag.rsplit('}', 1)[-1]
        if tag in tags and el.text:
            out.append(el.text)
        elif tag in {'tab'}:
            out.append('\t')
        elif tag in para_tags:
            out.append('\n')
    return re.sub(r'\n{3,}', '\n\n', ''.join(out)).strip()


def _docx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        parts = [n for n in z.namelist() if re.fullmatch(r'word/(document|header\d*|footer\d*|footnotes)\.xml', n)]
        parts.sort(key=lambda n: n != 'word/document.xml')
        return '\n\n'.join(_xml_text(z.read(n), {'t'}, {'p', 'br', 'tr'}) for n in parts)


def _pptx(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r'ppt/slides/slide\d+\.xml', n)),
                        key=lambda n: int(re.findall(r'\d+', n)[-1]))
        return '\n\n'.join(f'--- Слайд {i} ---\n' + _xml_text(z.read(n), {'t'}, {'p'})
                           for i, n in enumerate(slides, 1))


def _xlsx(data: bytes, max_rows: int = 2000) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        shared: list[str] = []
        if 'xl/sharedStrings.xml' in z.namelist():
            for si in ElementTree.fromstring(z.read('xl/sharedStrings.xml')):
                shared.append(''.join(t.text or '' for t in si.iter() if t.tag.endswith('}t')))
        names: dict[str, str] = {}
        if 'xl/workbook.xml' in z.namelist():
            wb = ElementTree.fromstring(z.read('xl/workbook.xml'))
            for i, sh in enumerate(el for el in wb.iter() if el.tag.endswith('}sheet')):
                names[f'xl/worksheets/sheet{i + 1}.xml'] = sh.get('name') or f'Лист{i + 1}'
        out = []
        sheets = sorted((n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)),
                        key=lambda n: int(re.findall(r'\d+', n)[-1]))
        for n in sheets:
            rows = []
            for row in ElementTree.fromstring(z.read(n)).iter():
                if not row.tag.endswith('}row'):
                    continue
                cells = []
                for c in row:
                    if not c.tag.endswith('}c'):
                        continue
                    v = next((x.text for x in c if x.tag.endswith('}v')), None)
                    if c.get('t') == 's' and v is not None and v.isdigit() and int(v) < len(shared):
                        v = shared[int(v)]
                    elif c.get('t') == 'inlineStr':
                        v = ''.join(t.text or '' for t in c.iter() if t.tag.endswith('}t'))
                    cells.append(v or '')
                if any(cells):
                    rows.append(' | '.join(cells))
                if len(rows) >= max_rows:
                    rows.append('… (строки обрезаны)')
                    break
            out.append(f'--- {names.get(n, n)} ---\n' + '\n'.join(rows))
        return '\n\n'.join(out)


def _odf(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return _xml_text(z.read('content.xml'), {'p', 'h', 'span', 's'}, {'p', 'h', 'table-row'})


def _pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return '[Для чтения PDF установите пакет pypdf: pip install pypdf]'
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages, 1):
        try:
            pages.append(f'--- Стр. {i} ---\n{page.extract_text() or ""}')
        except Exception as exc:
            pages.append(f'--- Стр. {i} --- [ошибка извлечения: {exc}]')
    text = '\n'.join(pages)
    if len(re.sub(r'\s|---.*?---', '', text)) < 20:
        return text + '\n[В PDF почти нет текстового слоя — вероятно, это скан. Нужен OCR.]'
    return text


def _eml(data: bytes) -> str:
    msg = email.message_from_bytes(data, policy=email.policy.default)
    head = '\n'.join(f'{h}: {msg.get(h)}' for h in ('From', 'To', 'Cc', 'Date', 'Subject') if msg.get(h))
    body = msg.get_body(preferencelist=('plain', 'html'))
    text = ''
    if body is not None:
        content = body.get_content()
        text = html_to_text(content) if body.get_content_type() == 'text/html' else content
    atts = [part.get_filename() for part in msg.iter_attachments() if part.get_filename()]
    if atts:
        text += '\n\nВложения: ' + ', '.join(atts)
    return f'{head}\n\n{text}'.strip()


def _rtf(data: bytes) -> str:
    raw = _decode(data)
    raw = re.sub(r"\\'([0-9a-f]{2})", lambda m: bytes([int(m.group(1), 16)]).decode('cp1251', 'replace'), raw)
    raw = re.sub(r'\\par[d]?', '\n', raw)
    raw = re.sub(r'\{\\\*[^{}]*\}|\\[a-z]+-?\d* ?|[{}]', '', raw)
    return re.sub(r'\n{3,}', '\n\n', raw).strip()


def extract_text(name: str, data: bytes) -> str:
    ext = PurePath(name.lower()).suffix
    if ext in IMAGE_EXT:
        return f'[Изображение {name}: текст не извлекается (нужен OCR или vision-модель)]'
    try:
        if ext in {'.docx', '.docm', '.dotx'}:
            return _docx(data)
        if ext in {'.xlsx', '.xlsm'}:
            return _xlsx(data)
        if ext == '.pptx':
            return _pptx(data)
        if ext in {'.odt', '.ods', '.odp'}:
            return _odf(data)
        if ext == '.pdf':
            return _pdf(data)
        if ext in {'.eml'}:
            return _eml(data)
        if ext == '.rtf':
            return _rtf(data)
        if ext in {'.html', '.htm'}:
            return html_to_text(_decode(data))
        if ext in {'.doc', '.xls', '.ppt'}:
            return (f'[{name}: старый двоичный формат Office — конвертируйте в {ext}x '
                    'или подключите конвертер (LibreOffice) в плагине]')
        if ext in {'.csv', '.tsv'}:
            text = _decode(data)
            rows = list(csv.reader(io.StringIO(text), delimiter='\t' if ext == '.tsv' else ';' if ';' in text[:2000]
                                   else ','))
            return '\n'.join(' | '.join(r) for r in rows[:3000])
        if ext in TEXT_EXT or not ext:
            return _decode(data)
        if zipfile.is_zipfile(io.BytesIO(data)):
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                return f'[Архив {name}] Состав: ' + ', '.join(z.namelist()[:200])
    except (zipfile.BadZipFile, ElementTree.ParseError, KeyError, ValueError) as exc:
        return f'[Не удалось разобрать {name}: {exc}]'
    sample = data[:4000]
    if sample and sum(32 <= b < 127 or b in (9, 10, 13) or b >= 128 for b in sample) / len(sample) > 0.95:
        return _decode(data)
    return f'[{name}: формат {ext or "без расширения"} не поддерживается для извлечения текста]'


def _int(v: object) -> int | None:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
