Подключение ИИ-агента к чатам Битрикс24
========================================

Этот раздел описывает, как зарегистрировать внешнего ИИ-агента (например, использующего
этот MCP-сервер как источник данных CRM) в качестве чат-бота Битрикс24, чтобы он:

* был доступен в **общих чатах** и его можно было добавить участником, как сотрудника;
* мог отвечать в **таймлайне** CRM-сущностей (лид/сделка/контакт) и получать там
  комментарии от менеджеров, как обычный сотрудник;
* мог использовать инструменты этого MCP-сервера (``get_contact``, ``search_contacts``,
  ``list_deals`` и т.д.) для подбора товаров и ведения контактной базы прямо во время диалога.

Помимо REST-доступа к CRM (``crm.contact.*``, ``crm.deal.*``), сервер содержит готовый
слой для подключения чат-бота (``imbot``), который можно использовать для регистрации
**любого количества** своих агентов без написания инфраструктурного кода с нуля:

* :mod:`src.infrastructure.bitrix.imbot_client` — обёртка над методами ``imbot.*`` и
  ``crm.timeline.comment.add``;
* :mod:`src.application.services.chat_bot` — ``ChatBotService``: регистрация бота,
  маршрутизация входящих событий к подключённым агентам, отправка ответов;
* :mod:`src.infrastructure.bot.webhook_server` — HTTP-сервер, принимающий вебхуки
  Bitrix24 и передающий их в ``ChatBotService``;
* :mod:`src.domain.interfaces.agent_handler` — единственный контракт
  (``AgentHandler.handle_message``), который должен реализовать ваш агент.

Чтобы подключить **своего** агента, достаточно реализовать ``AgentHandler`` и
вызвать ``chat_bot_service.register_agent(bot_id, MyAgent())`` — регистрацию бота в
портале, приём вебхуков и отправку ответов сервис берёт на себя. Готовый рабочий
пример — ``examples/custom_agent_example.py`` в корне репозитория (раздел
:ref:`ai_chat_agent_quickstart` ниже). Далее описано, что происходит "под капотом"
этого слоя и какие REST-методы Битрикс24 он использует.

.. _ai_chat_agent_quickstart:

Быстрый старт: подключить своего агента
------------------------------------------

.. code-block:: python

   from src.application.services.chat_bot import ChatBotService
   from src.domain.entities.bot_event import BotIncomingMessage, BotReply
   from src.infrastructure.ioc import provider

   class MySalesAgent:
       async def handle_message(self, message: BotIncomingMessage) -> BotReply:
           answer = await my_llm.ask(message.text)  # ваша LLM/агент
           return BotReply(text=answer)

   bitrix_client = provider.provide_bitrix_client()
   imbot_client = provider.provide_imbot_client(bitrix_client)
   chat_bot_service = ChatBotService(imbot_client)

   bot_id = await chat_bot_service.register_bot(
       code="my_agent",
       event_handler_url="https://your-domain.example.com/bot/events",
       name="Мой агент",
   )
   chat_bot_service.register_agent(bot_id, MySalesAgent())

После этого нужно поднять приём вебхуков —
``src.infrastructure.bot.webhook_server.run_webhook_server(chat_bot_service, ...)`` —
и указать его публичный адрес в ``event_handler_url`` выше. Полный рабочий скрипт со
всеми шагами: ``examples/custom_agent_example.py``.

Архитектура
-----------

.. code-block:: text

   Менеджер (чат/таймлайн Б24) ⇄ Битрикс24 (imbot API) ⇄ обработчик событий бота
                                                              │
                                                              ▼
                                              этот MCP-сервер (CRM tools) ⇄ LLM

Обработчик событий бота — это отдельный HTTP-сервис (можно реализовать как тонкую
обёртку, дёргающую инструменты текущего MCP-сервера в process). MCP-сервер сам не
принимает вебхуки Битрикс24 — он предоставляет LLM "инструменты" для работы с CRM.

1. Права доступа (scope)
-------------------------

В дополнение к существующему scope ``crm`` (используется этим сервером) для чат-бота
нужен scope ``imbot``. Создайте входящий вебхук с обоими правами или используйте
полноценное OAuth-приложение, если агент будет работать на нескольких порталах.

.. code-block:: bash

   # .env этого сервера уже содержит BITRIX_WEBHOOK_URL с правом crm.
   # Добавьте к этому же вебхуку (или отдельному) право imbot.
   export BITRIX_WEBHOOK_URL="https://your-domain.bitrix24.ru/rest/1/yoursecretcode/"

2. Сервер-обработчик событий бота
----------------------------------

Нужен публичный HTTPS-эндпоинт (либо fetch-режим ``imbot.v2.Event.get``, если нет
публичного домена). Обработчик должен:

* принимать POST-события от Битрикс24 (``ONIMBOTMESSAGEADD`` и т.п.);
* быстро отвечать ``200 OK``, тяжёлую обработку уводить в фон;
* проверять ``auth.access_token``/``application_token`` входящего запроса.

3. Регистрация бота (``imbot.register``)
------------------------------------------

.. code-block:: json

   POST https://{portal}/rest/imbot.register
   {
     "CODE": "ai_sales_assistant",
     "TYPE": "B",
     "EVENT_HANDLER": "https://your-server.example.com/bot/events",
     "EVENT_MESSAGE_ADD": "https://your-server.example.com/bot/events",
     "EVENT_WELCOME_MESSAGE": "https://your-server.example.com/bot/events",
     "EVENT_BOT_DELETE": "https://your-server.example.com/bot/events",
     "OPENLINE": "Y",
     "PROPERTIES": {
       "NAME": "AI Ассистент",
       "WORK_POSITION": "ИИ-помощник менеджера",
       "COLOR": "AQUA",
       "EMAIL": "ai-assistant@yourcompany.com"
     }
   }

* ``OPENLINE: "Y"`` делает бота доступным как участника открытых линий, а не только
  обычных чатов.
* В ответе приходит ``BOT_ID`` — он же ID пользователя-бота в портале. Этот ID
  используется как ``AUTHOR_ID`` при комментариях в таймлайне (см. ниже) — для
  Битрикс24 бот выглядит как обычный сотрудник.
* Все боты одного приложения должны использовать один и тот же набор URL обработчиков.

4. Команды бота (опционально)
-------------------------------

Чтобы менеджер мог явно вызывать конкретное действие (например, подбор товара),
зарегистрируйте команду через ``imbot.command.register`` (параметры: ``BOT_ID``,
``COMMAND``, ``COMMON``, ``EVENT_COMMAND_ADD``).

5. Обработка входящих сообщений
----------------------------------

Формат события ``ONIMBOTMESSAGEADD`` (упрощённо):

.. code-block:: json

   {
     "event": "ONIMBOTMESSAGEADD",
     "data": {
       "USER": {"ID": "45", "NAME": "Иван"},
       "MESSAGE": {
         "dialog_id": "chat778",
         "text": "Подбери ноутбук до 80к"
       }
     },
     "auth": {"access_token": "...", "domain": "yourportal.bitrix24.ru"}
   }

Обработчик должен:

1. Проверить подпись/``access_token``.
2. Достать ``dialog_id`` и ``text``.
3. При необходимости вызвать инструменты этого MCP-сервера (например,
   ``search_contacts`` по имени клиента из чата, ``list_deals`` для истории) либо
   инструменты каталога товаров, чтобы дать LLM контекст.
4. Передать текст и контекст LLM, получить ответ.
5. Отправить ответ методом ``imbot.message.add``:

.. code-block:: json

   {
     "BOT_ID": 123,
     "DIALOG_ID": "chat778",
     "MESSAGE": "Нашёл 3 варианта ноутбуков до 80 000 ₽: ...",
     "ATTACH": []
   }

``ATTACH`` поддерживает rich-карточки (заголовок, изображение, кнопки) — удобно для
подбора товаров (фото, цена, кнопка «Добавить в сделку»).

6. Доступность агента
-----------------------

* **Общие чаты:** после регистрации с ``OPENLINE: "Y"`` бот появляется в списке для
  добавления участником в любой чат — менеджер добавляет «AI Ассистент» как коллегу.
* **Открытые линии (диалог с клиентами напрямую):** отдельный механизм —
  ``imconnector.register`` → ``imconnector.activate`` → ``imopenlines.config.add``,
  приём ``OnImConnectorMessageAdd``, отправка через ``imconnector.send.messages``.
  Нужен только если агент должен сам отвечать клиентам в линии, а не только
  менеджерам в обычных чатах.
* **Таймлайн CRM (комментарии как от сотрудника):** ``crm.timeline.comment.add`` с
  ``AUTHOR_ID = BOT_ID``:

.. code-block:: json

   {
     "fields": {
       "ENTITY_ID": 4521,
       "ENTITY_TYPE": "deal",
       "AUTHOR_ID": 123,
       "COMMENT": "ИИ-агент: клиент интересовался моделью X, рекомендую апсейл Y."
     }
   }

  Чтобы агент реагировал на ответные комментарии менеджера в таймлайне, подпишитесь
  на событие ``OnCrmTimelineCommentAdd`` (``event.bind``) и обрабатывайте его так же,
  как ``ONIMBOTMESSAGEADD``.

7. Связка с инструментами этого MCP-сервера
----------------------------------------------

При обработке сообщения/комментария обработчик бота может напрямую дёргать те же
функции, что реализованы в :doc:`modules/application/index` этого сервера
(например, через переиспользование сервисов ``CrmService``), не дублируя логику
работы с CRM — таким образом ИИ-агент в чате и MCP-инструменты для LLM работают на
одном и том же слое доступа к Bitrix24.

Чек-лист безопасности
-----------------------

* Храните токен вебхука/бота в секретах, не в коде и не в репозитории (``.env`` уже
  в ``.gitignore`` этого проекта — не меняйте это).
* Проверяйте подпись каждого входящего вебхука от Битрикс24.
* Обработчик должен быть идемпотентным (Битрикс24 может повторять запрос при таймауте).
* Логируйте обращения с учётом того, что в сообщениях чата может быть PII клиентов.

Источники
-----------

* `Чат-боты 2.0: быстрый старт <https://apidocs.bitrix24.ru/api-reference/chat-bots/chat-bots-v2/quick-start.html>`_
* `imbot.register <https://apidocs.bitrix24.ru/api-reference/chat-bots/outdated/bots/imbot-register.html>`_
* `imbot.message.add <https://apidocs.bitrix24.ru/api-reference/chat-bots/messages/imbot-message-add.html>`_
* `imbot.command.register <https://apidocs.bitrix24.ru/api-reference/chat-bots/commands/imbot-command-register.html>`_
* `imconnector (открытые линии) <https://apidocs.bitrix24.ru/api-reference/imopenlines/imconnector/index.html>`_
* `crm.timeline.comment.add <https://apidocs.bitrix24.ru/api-reference/crm/timeline/comments/crm-timeline-comment-add.html>`_
