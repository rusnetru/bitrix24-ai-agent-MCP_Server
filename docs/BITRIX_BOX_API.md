# Битрикс24 REST API для MCP в закрытом контуре (коробка): справочник

Справочник собран по официальной документации [apidocs.bitrix24.ru](https://apidocs.bitrix24.ru/) 15.11.2026 и отобран под задачу: ИИ-агент или MCP-сервер работает с **коробочным** Битрикс24 через **входящий вебхук** в **закрытой сети**. Он описывает только то, что нужно для доработки агента. Полное описание параметров каждого метода смотрите на странице метода.

**Обозначения:**

| Знак | Смысл |
|---|---|
| ✅ | уже реализовано инструментом `b24agent` |
| 🟡 | доступно через `bitrix_call` (REST 2 или 3.0); кандидат в отдельный инструмент |
| ⛔ | работает только в контексте приложения (OAuth); через вебхук вернёт `WRONG_AUTH_TYPE` |
| ⚠ | важная особенность или ловушка |

> Коробка обновляется позже облака, и администратор сам решает, когда ставить обновления. Перед использованием метода проверяйте его наличие: `method.get` (инструмент `bitrix_method_info`) возвращает `isExisting` и `isAvailable`. Для imbot.v2 версию показывает `imbot.v2.Revision.get`, для REST 3.0 — `rest.documentation.openapi`.

---

## 1. Закрытый контур: что работает, а что нет

### 1.1. Входящий вебхук — основной способ авторизации

- **Где создать:** Разработчикам → Готовые сценарии → Другое → Входящий вебхук. Кому разрешено создавать вебхуки, задаётся в разделе Настройки → Безопасность → Интеграции Битрикс24.
- **URL:** `https://{портал}/rest/{user_id}/{код}/{метод}[.json]`. В продуктиве используйте только POST: GET оставляет секрет в логах.
- **Права** складываются из выбранных scope и прав пользователя-создателя. Код вебхука видит только его создатель. Вебхуку можно задать срок действия.
- ⚠ Если админ отредактирует чужой вебхук, код поменяется, и владельцем станет админ.
- ⚠ При увольнении создателя можно выбрать «Сохранить интеграции и уволить». Тогда вебхук переходит к системному пользователю и продолжает работать у всех, кто знает URL.
- **Рекомендация:** заведите отдельного сервисного пользователя «ИИ-агент» и создайте вебхук от него. Комментарии в таймлайне, задачи и согласования будут идти от этого пользователя, а его права ограничат агента.
- **Через вебхук недоступно:**
  - ⛔ `event.bind`, `event.offline.*`;
  - ⛔ `placement.bind` (встройки в интерфейс);
  - ⛔ `entity.*`, `bizproc.robot.*`, `bizproc.activity.*`, `crm.automation.trigger.execute`, `imconnector.*`;
  - ⛔ часть методов телефонии.

### 1.2. События в закрытом контуре

| Способ | Работает в закрытом контуре? | Комментарий |
|---|---|---|
| `event.bind` через вебхук | ⛔ нет | `WRONG_AUTH_TYPE` |
| Офлайн-события `event.offline.get` | ⛔ только приложение и админ | очередь хранит последнее состояние объекта, а не историю |
| Исходящий вебхук (UI) | ⚠ скорее нет | События идут через облачную очередь Битрикс24 (серверы `mp_actions-*`). Обработчик должен быть публичным: не localhost, не LAN, без самоподписанного сертификата. Нужна активная лицензия. Повторной доставки нет. |
| **imbot.v2 `eventMode: fetch`** | ✅ да | Агент сам забирает события бота методом `imbot.v2.Event.get` и подтверждает их через `offset`. События хранятся 24 часа. |
| Опрос `crm.timeline.comment.list` | ✅ да | только по конкретному элементу: фильтр принимает **только** `ENTITY_ID` + `ENTITY_TYPE` |
| Опрос `crm.item.list` по `lastActivityTime` | ✅ да (в `b24agent`) | находит недавно активные элементы, после чего читаются их комментарии |

### 1.3. Сеть и зависимости коробки

- Для приложений и OAuth коробке нужен исходящий HTTPS на `oauth.bitrix24.tech`. Через этот хост проходят установка приложений, выдача и обновление токенов и пересылка событий.
- ⚠ В документации сказано, что при блокировке этого хоста REST коробки перестаёт работать, потому что токены нельзя проверить. Для входящего вебхука OAuth в описании не упоминается. **Проверьте на своей сети:** заблокируйте хост на стенде и вызовите `profile` через вебхук.
- Для полностью изолированной сети есть **свой провайдер авторизации**: модуль в `local/modules/` с обработчиком `onRestCheckAuth`. Документация называет это крайней мерой.
- Раздел «Разработчики» обращается к `*.1c-bitrix.ru`, список событий в интерфейсе — к `www.bitrix24.*`.
- **Лицензия:** REST требует подписку (Маркетплейс) или пробный период. Без неё возвращается `ACCESS_DENIED` «REST is available only by subscription». Пробный период в коробке требует активной лицензии.
- REST работает **только по HTTPS** (`INVALID_REQUEST` «Https required»). Самоподписанный сертификат портала задаётся в агенте через `BITRIX_VERIFY_SSL=путь_к_CA.pem`.
- **Свои REST-методы (только коробка):** обработчик `OnRestServiceBuildDescription` в `/bitrix/php_interface/init.php`. Список scope кэшируется на 7 дней, сброс — `\Bitrix\Rest\Engine\ScopeManager::cleanCache()`. Так можно дать агенту доступ к своим таблицам или модулям.

### 1.4. Официальные AI-инструменты Битрикс24

- **`b24-dev-mcp`** (`https://mcp-dev.bitrix24.tech/mcp`, Streamable HTTP, без авторизации) отдаёт **только документацию**: поиск методов, событий, статей. Данные портала он не читает. В закрытом контуре нужен исходящий доступ; альтернатива — локальная копия документации.
- **Vibecode on-premise** (модуль `vibecodeconnector`) требует, чтобы коробка была доступна из интернета, и исходящий доступ к `vibecode.bitrix24.tech`. **Для закрытого контура не подходит.**

---

## 2. Правила вызова REST

| Тема | Правило |
|---|---|
| Формат | POST, `Content-Type: application/json`. Form-urlencoded тоже принимается. Ответ: `{"result": ..., "total": N, "next": 50, "time": {...}}` |
| Ошибка (REST 2) | `{"error": "CODE", "error_description": "..."}`. Ориентируйтесь на `error`, а не на HTTP-статус. Поле `error` бывает пустым: «Not found» приходит с HTTP 400. |
| Списки `*.list` | По 50 элементов на страницу. `start` = значение `next` из предыдущего ответа; если `next` нет, страница последняя. `total` есть не везде. |
| Большие выборки | `start=-1` (без подсчёта total) + фильтр `>ID` по последнему ID + сортировка по ID ASC; остановка, когда пришло меньше 50 записей. Пример из документации: 49,9 с против 0,098 с. |
| Позиционные параметры | `task.commentitem.*`, `task.checklistitem.*`, `task.elapseditem.*` читают параметры **по порядку** таблицы параметров |
| Даты | `date` = `YYYY-MM-DD`, `datetime` = ISO 8601 с часовым поясом; хранятся во времени сервера. ⚠ Встречаются и форматы `24.08.2023 13:19:39`, `2024-08-29 10:30:11`. Сдвиг часов проверяйте через `server.time`. |
| Булевы | обычно `Y`/`N`; бывают `1`/`0` и `true`/`false`. В REST 3.0 — только `true`/`false`. |
| Файлы | Base64 без префикса `data:`. Формат зависит от метода: `[имя, base64]`, `{"fileData": [имя, base64]}`, `fileContent`, ID файла на Диске. Подробности в разделе 9. |
| Лимиты | Лимиты частоты и ресурсоёмкости (`QUERY_LIMIT_EXCEEDED`, leaky bucket) действуют **в облаке**. В коробке нагрузку настраивают на сервере. `OPERATION_TIME_LIMIT` в коробке выключен по умолчанию; если его включить, значение по умолчанию — 420 с (настройки модуля rest). Повторы с экспоненциальной задержкой в `b24agent` уже есть. |
| batch (v2) | `{"halt":0,"cmd":{"a":"crm.item.get?entityTypeId=2&id=1","b":"user.get?ID=$result[a][item][assignedById]"}}`. До 50 команд; вложенный batch запрещён с rest 24.0.0. HTTP 200 приходит даже при ошибках: смотрите `result_error`. |
| batch (v3) | `POST /rest/api/.../batch`, тело — JSON-массив `[{"method","query","as"}]`, ссылки `{"$ref":"name.field"}`. Типы строгие. |

### 2.1. Ключевые коды ошибок

| Код | Что делать |
|---|---|
| `NO_AUTH_FOUND`, `INVALID_CREDENTIALS` | неверный или удалённый вебхук |
| `insufficient_scope` | добавить scope вебхуку |
| `ACCESS_DENIED` | нет прав у пользователя вебхука; или «REST only by subscription» |
| `WRONG_AUTH_TYPE` | метод доступен только приложению |
| `ERROR_METHOD_NOT_FOUND` | нет модуля, неверное имя или для REST 3.0 не указан `/api/` |
| `PORTAL_DELETED` | в коробке выключите «Временное закрытие публичной части сайта» (Главный модуль) |
| `QUERY_LIMIT_EXCEEDED` / `OPERATION_TIME_LIMIT` | повторить с задержкой; для второго дождаться `operating_reset_at` |
| `INTERNAL_SERVER_ERROR` | повторить, затем смотреть логи портала |
| REST 3.0 | `BITRIX_REST_V3_EXCEPTION_*`: `VALIDATION_*`, `ACCESSDENIEDEXCEPTION`, `ENTITYNOTFOUNDEXCEPTION`, `INVALIDFILTEREXCEPTION`, `METHODNOTFOUNDEXCEPTION` и др. |

---

## 3. REST 3.0

- **URL:** `https://{портал}/rest/api/{user_id}/{код}/{метод}`. Только POST и JSON; параметры из query string не читаются. В `b24agent`: `ctx.b24.call(method, params, v3=True)` или `bitrix_call(api_version="3")`.
- **Ответ:** `{"result": {"item": ...} | {"items": [...]}}`. **`total` нет**: для подсчёта используйте `*.aggregate`.
- **Ошибка:** `{"error": {"code", "message", "validation": [...]}}`.
- **Фильтр:** массив условий `[["field","op",value]]`. Операторы: `=, !=, >, >=, <, <=, in, between`; **LIKE нет**. Группы: `{"type":"filter","logic":"or","conditions":[...]}`.
- **Выборка:** `select` обязателен, `*` нет, связанные поля указываются через точку (`assignedBy.firstName`).
- **Пагинация:** `pagination: {limit ≤ 1000, page | offset}`.
- **Идемпотентность:** заголовок `Idempotency-Key` (1–255 ASCII). Повторная отправка в течение 24 часов вернёт сохранённый ответ.
- **Что покрыто:**
  - CRM: `crm.deal|lead|contact|company|quote|smartinvoice|smartprocess<entityTypeId>.*`, поля, категории и стадии, товарные позиции, реквизиты, адреса, дела (только чтение и статус), справочники `crm.dictionary.*`;
  - задачи `tasks.task.*` (3.0), база знаний `note.*`, почта, каталог, `humanresources.*`, `main.eventlog.*`, `timeman.record.*`;
  - `iblock.*` — только для инфоблоков с включённым REST.
- **Остаются в REST 2:** `crm.type.*`, `crm.item.*`, таймлайн, документогенератор, автоматизация, валюты, `crm.stagehistory.*`.
- **Обнаружение:** `rest.documentation.openapi` (OpenAPI конкретного портала), `rest.scope.list`.

---

## 4. Общие методы (scope `base`)

| Метод | Назначение |
|---|---|
| `profile` ✅ | пользователь вебхука (ID, ADMIN, имя) |
| `scope` ✅ | scope вебхука (`full: true` — все scope портала) |
| `method.get` ✅ | `{isExisting, isAvailable}` для метода; имя в нижнем регистре |
| `server.time` ✅ | время сервера портала (для курсоров и сдвига часов) |
| `app.info` 🟡 | через вебхук: `{"SCOPE":[...], "LICENSE":"ru_selfhosted"}` |
| `user.access`, `access.name` 🟡 | проверка кодов доступа (`U1`, `D5`, `SG3_A` …) и их названия |
| `feature.get` 🟡 | доступность функций (на коробке `ru_selfhosted`) |
| `methods` | устарел и неполон, используйте `method.get` |

---

## 5. CRM

### 5.1. Универсальный API `crm.item.*` ✅

- **Методы:** `crm.item.add/update/get/list/delete/fields`, `crm.item.batchImport` (до 20 за раз), `crm.item.productrow.*`.
- ⚠ В документации `crm.item.add` и `crm.item.list` помечены как DEPRECATED в пользу REST 3.0 `crm.<type>.*`, но продолжают работать. Для коробок без REST 3.0 это единственный универсальный путь.
- **Параметры:** `entityTypeId` (обязателен), `select` (`*`; поле `fm` с мультиполями — только через `*`), `filter`, `order`, `start`, `useOriginalUfNames` (`N` → `ufCrm2_...`, `Y` → `UF_CRM_2_...`).
- **Операторы фильтра:** `>=, >, <=, <, @ (in), !@, %, =%, %=, !%, =, !=, !`. Логическое ИЛИ: `{"logic":"OR", ...}` во вложенной группе. Булево пользовательское поле в фильтре — `1`/`0`.

| entityTypeId | Тип | Префикс задачи (UF_CRM_TASK) | ENTITY_TYPE таймлайна |
|---|---|---|---|
| 1 | Лид | `L_` | `lead` |
| 2 | Сделка | `D_` | `deal` |
| 3 | Контакт | `C_` | `contact` |
| 4 | Компания | `CO_` | `company` |
| 7 | Предложение | `Q_` | `quote` |
| 31 | Счёт (новый) | `SI_` | `smart_invoice` |
| 36 / 39 | Смарт-документ / документ B2E | — | — |
| ≥128 | **Смарт-процессы** (`crm.type.list`) | `T{hex(entityTypeId)}_` (например `T80_6` для 128) | `dynamic_{entityTypeId}` |

### 5.2. Смарт-процессы ✅

- `crm.type.list/get/getByEntityTypeId/add/update/delete/fields`.
- ⚠ `list` требует прав администратора (у `b24agent` есть запасной вариант с кэшем).
- ⚠ Поле `id` типа отличается от `entityTypeId`; в именах методов REST 3.0 используется `entityTypeId`.
- **Флаги типа:** `isCategoriesEnabled`, `isStagesEnabled`, `isClientEnabled`, `isLinkWithProductsEnabled`, `isObserversEnabled`, `isAutomationEnabled`, `isBizProcEnabled`, `isUseInUserfieldEnabled` и др.
- **Связи** родитель/потомок задаются в `relations` типа.
- **События:** `onCrmDynamicItemAdd/Update/Delete` (для смарт-процессов и новых счетов) — ⛔ подписка только через приложение.

### 5.3. Воронки и стадии ✅

- **Категории:** `crm.category.list {entityTypeId}` (помечен DEPRECATED, работает) или REST 3.0 `crm.<type>.category.list`.
- **Стадии:** `crm.status.list {filter: {ENTITY_ID}}` (DEPRECATED, работает). REST 3.0: `crm.<type>.category.stage.list` с фильтром `["categoryId","=",N]`; там же цвет и semantics `process/success/failure`.
- **`ENTITY_ID` для стадий:**
  - сделки: `DEAL_STAGE` (воронка 0), `DEAL_STAGE_{cat}`;
  - лиды: `STATUS`;
  - предложения: `QUOTE_STATUS`;
  - смарт-процессы: `DYNAMIC_{etid}_STAGE_{cat}`;
  - счета: `SMART_INVOICE_STAGE_{cat}`.
- **Справочники:** `SOURCE`, `DEAL_TYPE`, `CONTACT_TYPE`, `COMPANY_TYPE`, `INDUSTRY`, `HONORIFIC`. `crm.status.entity.types` возвращает все типы справочников.
- **История стадий:** `crm.stagehistory.list` ✅ (лиды, сделки, счета, смарт-процессы).

### 5.4. Пользовательские поля

- **Чтение:** `crm.item.fields` ✅ возвращает `title`, `type`, `isRequired`, `isMultiple`, `items` (варианты списка), `upperName`.
- **Создание:** REST 3.0 `crm.<type>.field.custom.add/list/update/delete` и `field.custom.enum.*`. ⚠ Поля типов «список» и «файл» в REST 3.0 не создаются: используйте `crm.<type>.userfield.add` (REST 2).
- **Форматы значений:**
  - файл — `[имя, base64]`;
  - привязка к CRM в REST 2 — `"D_1"`, `"T80_6"`, в REST 3.0 — `{"entityTypeId","entityId"}`;
  - адрес в REST 2 — `"addr|lat;lng|id"`.

### 5.5. Таймлайн ✅

- **`crm.timeline.comment.add`:**
  - поля: `fields{ENTITY_ID, ENTITY_TYPE | ENTITY_TYPE_ID, COMMENT, FILES:[[имя, base64]]}`;
  - ⚠ ключ `fields` пишется строчными буквами (с CRM 23.100.0);
  - ⚠ автор — **текущий пользователь вебхука**; поля AUTHOR_ID в документации нет;
  - требуются права на изменение элемента.
- **`crm.timeline.comment.list`:** фильтр **только** `ENTITY_ID` + `ENTITY_TYPE` (`CREATED`, `>ID` игнорируются); сортировка только `ID/CREATED/AUTHOR_ID`; страница 50.
- **`crm.timeline.comment.get`:** `ID, ENTITY_ID, ENTITY_TYPE, CREATED, COMMENT, AUTHOR_ID, FILES{urlDownload...}`.
- **Упоминания** в тексте комментария имеют вид `[USER=ID]Имя[/USER]`.
- **Методы-кандидаты (🟡):**
  - заметки: `crm.timeline.note.save/get/delete`;
  - лог-сообщения: `crm.timeline.logmessage.add/list` — служебные записи агента, например «ИИ проанализировал сделку»;
  - закрепление: `crm.timeline.item.pin/unpin`;
  - привязки комментария к нескольким объектам: `crm.timeline.bindings.bind/list/unbind`.

### 5.6. Дела

- **`crm.activity.todo.add` ✅:** `ownerTypeId, ownerId, deadline` (обязательно, ISO), `title, description` (BB-коды), `responsibleId, pingOffsets[], colorId`. Связанные методы: `todo.update`, `updateDeadline`, `updateDescription`, `updateResponsibleUser`.
- **`crm.activity.list` ✅** (чтение в `crm_timeline`): типы 1 — встреча, 2 — звонок, 3 — задача, 4 — письмо, 6 — провайдер; `crm.activity.list` без фильтра по владельцу доступен только админу.
- **🟡 Кандидаты:**
  - `crm.activity.call.getTranscript` — расшифровка звонка, полезно для суммаризации;
  - `crm.activity.mail.getContent` / `getThread` — содержимое письма и переписка;
  - `crm.activity.binding.*`.

### 5.7. Контакты, компании, дубли, реквизиты

- **Мультиполя:** `PHONE/EMAIL/WEB/IM` — массивы `{VALUE, VALUE_TYPE}` (через `crm.contact.*`) ✅. Без `VALUE_TYPE` значение сбрасывается в `WORK`.
- **Дубли:** `crm.duplicate.findbycomm {type: PHONE|EMAIL, values[], entity_type}` ✅ ищет только среди лидов, контактов и компаний.
- **Связи:** `crm.deal.contact.*`, `crm.contact.company.*` (🟡); в REST 3.0 — поля `contactIds`, `companyId`.
- **🟡 Реквизиты:** `crm.requisite.*`, `.bankdetail.*`, `crm.address.*` (ИНН, КПП, адреса; агент может проверять и заполнять карточки).
- ⚠ **Объединение дублей:** `crm.entity.mergeBatch` — операция опасная, в `bitrix_call` закрыта по умолчанию.

### 5.8. Прочее CRM

- 🟡 `crm.enum.ownertype` (соответствие entityTypeId коду), `crm.currency.list`, `crm.settings.mode.get`.
- ⛔ `crm.automation.trigger.execute` — только приложение и админ.
- **🟡 Документогенератор `crm.documentgenerator.*`:**
  - `template.list` → `document.add {templateId, entityTypeId, entityId}`;
  - ссылка для скачивания: `downloadUrlMachine`;
  - PDF формируется асинхронно, проверять через `document.get`.

---

## 6. Задачи (scope `task`) ✅

- **Основные методы:** `tasks.task.add/update/get/list/delete`; статусы: `start/pause/defer/complete/renew`; `delegate/approve/disapprove`; `history.list`, `counters.get`, `getaccess`.
- **`tasks.task.list`:**
  - `select` передавайте всегда;
  - фильтр — поля ЗАГЛАВНЫМИ (`RESPONSIBLE_ID, REAL_STATUS, %TITLE, <DEADLINE, GROUP_ID, UF_CRM_TASK, ...`);
  - избранное — `::SUBFILTER-PARAMS: {FAVORITE: Y}`;
  - ответ — `result.tasks` + `total`;
  - ⚠ поля в ответе в camelCase.
- **Статусы:** 2 — ждёт, 3 — выполняется, 4 — ждёт контроля, 5 — завершена, 6 — отложена; мета-статусы `-1` — просрочена, `-2` — не просмотрена, `-3` — почти просрочена.
- **`tasks.task.add`:**
  - обязательны `TITLE`, `RESPONSIBLE_ID`; также `DEADLINE` (ISO), `UF_CRM_TASK: ["D_10","T80_6"]`, `UF_TASK_WEBDAV_FILES: ["n<diskFileId>"]`;
  - `SE_PARAMETER` задаёт правила: сроки по подзадачам, автозавершение, «не завершать без результата».
- **Комментарии:**
  - `task.commentitem.*` (позиционные параметры) ✅;
  - ⚠ в новой карточке задачи (tasks 25.700+) обсуждение переехало в **чат задачи**: `tasks.task.chat.message.send` 🟡, затем `im.dialog.messages.get`.
- **🟡 Кандидаты:**
  - чек-листы `task.checklistitem.*`;
  - учёт времени `task.elapseditem.*`;
  - результаты `tasks.task.result.*`;
  - стадии канбана и «Моего плана» `task.stages.*`;
  - шаблоны `tasks.template.*`;
  - потоки `tasks.flow.Flow.*`;
  - скрам `tasks.api.scrum.*`.
- **REST 3.0:** `tasks.task.*` (ответ `result.items`, пагинация, select связанных полей `responsible.name`).

---

## 7. Чаты и боты

### 7.1. imbot.v2 ✅ (рекомендуется; v1 «развитие остановлено»)

- **Авторизация через вебхук:** `botId` + `botToken` (произвольная строка до 40 символов, задаётся при регистрации). `CLIENT_ID` не используется. Scope `imbot`.
- **`imbot.v2.Bot.register`:**
  - формат вызова: `{"fields":{"code","botToken","properties":{"name","lastName","workPosition","color","avatar"},"type","eventMode","webhookUrl"?}}`;
  - повторная регистрация с тем же `code` вернёт существующего бота, **не обновляя** его; обновление — через `Bot.update`;
  - ответ: `result.bot.id`.
- **Типы бота:**
  - `bot` — личные сообщения, @упоминания и ответы на сообщения бота;
  - `supervisor` — все сообщения в чатах, где он состоит;
  - `personal` — то же, что `supervisor`, но поиск ограничен группами пользователей;
  - `openline`.
- **`eventMode`:**
  - `fetch` (по умолчанию) — «для AI-агентов … без постоянного HTTP-сервера»;
  - `webhook` — портал сам отправляет POST на `webhookUrl`; вызывать `event.bind` не нужно.
- **`imbot.v2.Event.get {botId, botToken, offset, limit≤1000}`** → `{events:[{eventId,type,date,data}], nextOffset, hasMore}`:
  - подтверждение — следующий вызов с `offset = nextOffset`;
  - события хранятся 24 часа;
  - **одна очередь на бота**: два агента на одном боте отнимают друг у друга события.
- **Сообщения:**
  - `imbot.v2.Chat.Message.send {botId, botToken, dialogId, fields:{message, keyboard, attach, replyId, system, urlPreview}}` — до 20 000 символов;
  - `Chat.Message.update/delete/get/getContext`, `Reaction.add`.
- **Индикатор «печатает»:** `imbot.v2.Chat.InputAction.notify {dialogId, statusMessageCode: IMBOT_AGENT_ACTION_THINKING|SEARCHING|GENERATING|ANALYZING|..., duration≤600}`.
- **Чаты:**
  - `imbot.v2.Chat.add {fields:{title, description, userIds, type: chat|open}}` → `result.chat.dialogId`;
  - ⚠ привязать такой чат к объекту CRM нельзя: полей `entityType`/`entityId` нет; `b24agent` хранит привязку у себя;
  - участники и управление: `Chat.User.add/delete/list`, `Chat.leave`, `Chat.Manager.*`, `Chat.setOwner`.
- **Команды:** `imbot.v2.Command.register/answer` (меню и слэш-команды); файлы — `imbot.v2.File.upload/download` (до 100 МБ).
- **События:**
  - `ONIMBOTV2MESSAGEADD`: `data.bot`, `data.message{id, chatId, authorId, text, params}`, `data.chat{id, dialogId, type: private|chat|…, entityType, entityId}`, `data.user{id, name, bot, …}`;
  - `ONIMBOTV2JOINCHAT`, `ONIMBOTV2COMMANDADD`, `ONIMBOTV2REACTIONCHANGE`, `ONIMBOTV2MESSAGEUPDATE/DELETE`, `ONIMBOTV2DELETE`;
  - ⚠ в режиме webhook все скаляры приходят строками, а пустые значения отсутствуют.
- **Версия:** `imbot.v2.Revision.get` → `{"rest": 33, ...}`. Перед использованием новых полей сверяйтесь с change-log.
- **Лимиты:** 2 запроса/с на приложение (429), 100 ботов на приложение.

### 7.2. imbot v1 (устаревший, поддержан в `b24agent` через `BOT_API=v1`)

- Работает только в push-режиме. Регистрация: `imbot.register` с `CLIENT_ID`, который обязателен для вебхуков.
- Событие `ONIMBOTMESSAGEADD` содержит `data.PARAMS{DIALOG_ID, MESSAGE, MESSAGE_ID, FROM_USER_ID, CHAT_TYPE, CHAT_ENTITY_TYPE, CHAT_ENTITY_ID, ...}`.
- Типы бота: `B`, `S` (supervisor), `O`, `H`.

### 7.3. im (scope `im`)

- **`DIALOG_ID`:** `"123"` — личный диалог с пользователем, `"chat123"` — групповой чат, `"sg17"` — чат группы.
- **Чат объекта:** `im.chat.get {ENTITY_TYPE, ENTITY_ID}` возвращает ID штатного чата:
  - CRM: `ENTITY_TYPE=CRM`, `ENTITY_ID=DEAL|1663` или `DYNAMIC_1038|5`;
  - также `TASKS_TASK`, `SONET_GROUP`, `CALENDAR`;
  - 🟡 кандидат в инструмент «найти чат сделки и добавить в него бота».
- 🟡 `im.dialog.messages.get` (история чата, полезна для суммаризации), `im.recent.list`, `im.chat.user.add`.
- 🟡 **Уведомления** `im.notify.personal.add` / `im.notify.system.add` — личное или системное уведомление сотруднику от агента, например напоминание.
- **Файлы:** `im.disk.file.commit` устарел, используйте `im.v2.File.upload` / `imbot.v2.File.upload`.

### 7.4. Открытые линии и лента

- **🟡 Открытые линии:** `imopenlines.bot.session.message.send/operator/finish` (бот на линии, передача оператору), `imopenlines.crm.chat.get`.
- **🟡 Лента:** `log.blogpost.add/get`, `log.blogcomment.add` — публикация дайджестов от агента.

---

## 8. События (для справки)

- `event.bind` / `event.unbind` / `event.get` ⛔ — только приложение. Требования к `handler`: http(s), хост с точкой, не localhost.
- **Офлайн-события** ⛔ — только приложение и админ: `event.offline.get(clear=0)` → обработка → `event.offline.clear`.
- **Безопасный обработчик:** сравнивайте `auth[application_token]` с сохранённым токеном и возвращайте 403 при несовпадении. В imbot.v2 webhook используется верхнеуровневый `auth.application_token`. В `b24agent` для push-режима есть дополнительная защита `EVENT_SECRET`.
- **Полезные CRM-события** (если будет приложение): `onCrmDealAdd/Update`, `onCrmDynamicItemAdd/Update`, `onCrmTimelineCommentAdd`, `onCrmActivityAdd`, `OnTaskAdd/Update/CommentAdd`, `OnCalendarEntryAdd`.

---

## 9. Диск и файлы

- **🟡 Диск:** `disk.storage.getList`, `disk.folder.getChildren`, `disk.folder.uploadFile {id, data{NAME}, fileContent:[имя, base64], generateUniqueName}`, `disk.file.get` → `DOWNLOAD_URL`.
- **Скачивание через вебхук:** взять `DOWNLOAD_URL` из `disk.file.get` и сделать GET с `Referer: https://портал/`. Ссылки бывают одноразовыми и с ограниченным сроком.
  - ⚠ `DETAIL_URL`, `urlDownload` открываются только в браузерной сессии и вернут HTML.
- **Привязка файлов:**
  - задача — `UF_TASK_WEBDAV_FILES: ["n<id>"]` или `tasks.task.files.attach`;
  - комментарий таймлайна — `FILES: [[имя, base64]]`;
  - чат — `imbot.v2.File.upload`.
- **Чтение файлов:** поле-файл в `crm.item.get` возвращает `{id, url, urlMachine}`; `urlMachine` подходит для серверной загрузки.
- **Кандидат в инструмент:** «прочитать вложение» — скачать файл и извлечь текст из PDF или DOCX для LLM.

---

## 10. Процессы, списки, данные

- **Бизнес-процессы** ✅:
  - `bizproc.task.list` (свои задания; админ видит все), `bizproc.task.complete {TASK_ID, STATUS 1/2/3/4, COMMENT, FIELDS}`;
  - `bizproc.workflow.start {TEMPLATE_ID, DOCUMENT_ID:["crm","CCrmDocumentDeal","DEAL_1"] | ["crm","Bitrix\\Crm\\Integration\\BizProc\\Document\\Dynamic","DYNAMIC_147_1"] | ["lists","BizprocDocument","777"], PARAMETERS}` — только админ;
  - 🟡 `bizproc.workflow.instances`, `bizproc.workflow.terminate`.
- **Универсальные списки** ✅:
  - `IBLOCK_TYPE_ID`: `lists`, `bitrix_processes` (процессы в ленте), `lists_socnet` (+ `SOCNET_GROUP_ID`);
  - `lists.element.add` требует `ELEMENT_CODE`; множественные свойства передаются массивом;
  - 🟡 `lists.element.update/delete`, `lists.element.get.file.url`.
- `entity.*` ⛔ — хранилище приложения, через вебхук недоступно; используйте SQLite агента или свой REST-метод.
- **🟡 Каталог:** `catalog.catalog.list` → `catalog.product.list {select ⊇ [id, iblockId], filter{iblockId}}`, `catalog.price.list`, `catalog.section.list`.
- **🟡 База знаний 2.0 (`note.*`, только REST 3.0):** `note.collection.*`, `note.document.add/update{markdown}/get/list/search.list`. Агент может искать ответы в базе знаний или вести FAQ.
- **BitrixGPT `ai.engine.*`** (scope `ai_admin`) — обратная интеграция: зарегистрировать корпоративную LLM как движок CoPilot внутри Битрикс24. Требование: `completions_url` должен отвечать порталу 202 за 5 с, а результат приходит на callback. Можно поднять адаптер к LiteLLM.

---

## 11. Сотрудники, структура, календарь, время

- **Пользователи** ✅ `user.get` / `user.search`:
  - операторы фильтра: `=, !=, >, <, @, %`;
  - `UF_DEPARTMENT` — только прямые сотрудники подразделения;
  - `USER_TYPE: employee|extranet`, `IS_ONLINE`;
  - ⚠ неизвестные поля фильтра молча игнорируются.
- **Подразделения:**
  - `department.get` ✅ (`ID, NAME, PARENT, UF_HEAD`);
  - 🟡 REST 3.0 `humanresources.node.list/search/children`, `humanresources.employee.search/subordinates` (подчинённые, команды).
- **Группы и проекты** ✅ `socialnetwork.api.workgroup.list`:
  - `select` обязателен (по умолчанию только ID); ответ — `result.workgroups`;
  - `type`: `group|project|scrum|collab`.
- **Календарь** ✅:
  - `calendar.event.get {type: user|group|company_calendar, ownerId, from, to}`;
  - ⚠ повторяющиеся события возвращаются отдельными записями с одинаковым ID; `DATE_FROM` — в локальном формате портала;
  - `calendar.accessibility.get`, `calendar.event.add`;
  - 🟡 `calendar.meeting.status.set`, `calendar.resource.*` (переговорные).
- **🟡 Учёт времени `timeman.*`:** `status/open/pause/close` (свой рабочий день), `timeman.timecontrol.reports.get`; REST 3.0 `timeman.record.list`. Если инструмент отключён, методы вернут `TIMEMAN_TOOL_DISABLED`.
- **🟡 Журнал событий** (REST 3.0, только админ): `main.eventlog.list/tail` — аудит входов и действий, сценарии для безопасности.

---

## 12. Бэклог: что ещё стоит сделать инструментами

| Приоритет | Инструмент | Методы |
|---|---|---|
| высокий | Суммаризация звонков и писем по сделке | `crm.activity.list` + `crm.activity.call.getTranscript` + `crm.activity.mail.getContent` |
| высокий | Найти штатный чат объекта и добавить в него бота | `im.chat.get {ENTITY_TYPE: CRM, ENTITY_ID: DEAL\|id}` + `im.chat.user.add` |
| высокий | Комментарии в новой карточке задачи (чат задачи) | `tasks.task.chat.message.send`, `im.dialog.messages.get` |
| высокий | Чтение вложений (PDF, DOCX) | `disk.file.get` → `DOWNLOAD_URL`, `crm.item.get` → `urlMachine` |
| средний | Товары сделки | `crm.item.productrow.list/set`, `catalog.product.list` |
| средний | Реквизиты компании (ИНН, банк) | `crm.requisite.list`, `crm.requisite.bankdetail.list` |
| средний | Документ по шаблону (КП, договор) | `crm.documentgenerator.template.list`, `crm.documentgenerator.document.add` |
| средний | Чек-листы и учёт времени задач | `task.checklistitem.*`, `task.elapseditem.*` |
| средний | Напоминания сотрудникам | `im.notify.personal.add` |
| средний | Поиск по базе знаний | `note.document.search.list` (REST 3.0) |
| низкий | Служебные записи в таймлайне | `crm.timeline.logmessage.add` |
| низкий | Подчинённые и команды | `humanresources.employee.subordinates` (REST 3.0) |
| низкий | MCP по сети (Streamable HTTP) для внутренних агентных платформ | `mcp.server.streamable_http` |

---

## Источники

Основные страницы, прочитанные при подготовке (все — `https://apidocs.bitrix24.ru/`):

- `local-integrations/local-webhooks.html`, `local-integrations/local-apps.html`;
- `settings/cloud-and-on-premise/` (`network-access`, `on-premise/versions`, `custom-auth-provider`, `custom-methods`), `settings/system-user.html`, `settings/oauth/index.html`;
- `limits.html`, `error-codes.html`, `settings/how-to-call-rest-api/` (`general-principles`, `data-encoding`, `list-methods-pecularities`, `batch`, `batch-v3`), `settings/performance/huge-data.html`;
- `api-reference/rest-v3.html`, `api-reference/common/system/*`;
- `api-reference/crm/` (`universal/*`, `status/*`, `timeline/comments/*`, `timeline/activities/todo/*`, `rest-v3/*`, `duplicates/*`, `crm-stage-history-list.html`, `automation/*`, `migration.html`);
- `api-reference/tasks/*`, `api-reference/user/user-get.html`, `api-reference/departments/index.html`, `api-reference/sonet-group/*`, `api-reference/calendar/*`, `api-reference/timeman/index.html`, `api-reference/event-log/*`;
- `api-reference/chat-bots/chat-bots-v2/imbot.v2/*` (`bots`, `events`, `messages`, `chats`, `ui`, `revision-get`, `entities`), `api-reference/chat-bots/outdated/*`, `api-reference/chats/im-chat-get.html`, `api-reference/imopenlines/index.html`, `api-reference/log/index.html`;
- `api-reference/events/` (`event-bind`, `offline-events`, `safe-event-handlers`);
- `api-reference/disk/*`, `api-reference/files/*`, `api-reference/lists/*`, `api-reference/entity/index.html`, `api-reference/bizproc/*`, `api-reference/document-generator/*`, `api-reference/catalog/*`, `api-reference/note/index.html`, `api-reference/ai/index.html`;
- `ai-tools/mcp.html`, `ai-tools/vibecode-onpremise.html`, `api-reference/widgets/index.html`.

Часть страниц прочитана не полностью (длинные страницы и карта сайта). Перед реализацией нового инструмента откройте страницу метода и проверьте его на своей коробке через `method.get`.
