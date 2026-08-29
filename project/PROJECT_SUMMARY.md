# Bitrix24 AI Core — сводный статус проекта (на 28.08.2026)

Собрано из трёх документов: `core-architecture-checklist.md`, `STATUS.md`,
`CHECKPOINT_seller_telegram.md`. Разделено на 3 группы: **ядро** (общая
инфраструктура, от неё зависят оба модуля) и два независимых модуля —
**testing** (Bitrix) и **seller** (Bitrix + Telegram).

---

# 1. ЯДРО (core/, adapters/bitrix/ база, инфраструктура)

## 1.1 Общая идея

Одно универсальное мульти-клиентское ядро, не отдельное приложение на
каждого клиента:
- регистрируется **один раз** как маркетплейс-приложение в Bitrix24 (один
  `client_id`/`client_secret` на всех клиентов);
- обслуживает много порталов одновременно;
- каждому порталу включаются свои независимые **модули** через флаги в БД —
  код один и тот же, различие только в данных.

## 1.2 Слои

```
adapters/   — перевод внешнего протокола (Bitrix, Telegram, будущие каналы) во внутренний вызов задачи
services/   — чистая бизнес-логика фичи; не знает о канале и не знает о других фичах
tasks/      — мост между адаптером и очередью Celery
core/       — общая инфраструктура (БД, очередь, эмбеддинги); не знает ни про Bitrix, ни про фичи
```
- `services/__init__.py` — единый `MODULE_HANDLERS` registry: имя модуля →
  обработчик.
- Модули (`testing`, `seller`) не знают друг о друге и не знают про Bitrix
  напрямую — только через `adapters/bitrix/`.

## 1.3 Данные (`core/models.py`)

**`Client`** — один портал Bitrix24:
- `member_id` — PK, неизменяемый ID портала.
- `domain` — `unique=True` (на него ссылается `TestSession` как на FK).
- `access_token` / `refresh_token` / `application_token`.
- `enabled_modules` — список строк, валидируется по `ALLOWED_MODULES`,
  дефолт — пустой список (активация — всегда отдельное осознанное действие).
- `bot_ids` — на каждый активный модуль отдельная запись `{"id": int, "token": str}`.
- `is_active` — `False` после `ONAPPUNINSTALL`, запись не удаляется физически.

Токены OAuth обновляются через **optimistic CAS**
(`UPDATE ... WHERE refresh_token = <старое значение>`), без `FOR UPDATE`, —
чтобы медленный ответ `oauth.bitrix.info` не блокировал остальные вебхуки
того же клиента.

## 1.4 Реальный формат вебхуков Bitrix (подтверждено на живом портале)

**`ONAPPINSTALL`** — form-urlencoded, вложенность в имени ключа:
```
event=ONAPPINSTALL
auth[access_token], auth[refresh_token], auth[domain],
auth[member_id], auth[application_token], auth[expires_in], auth[scope]
```
Не `DOMAIN`/`MEMBER_ID`/`AUTH_ID`/`REFRESH_ID`/`APP_SID` верхнего уровня, как
было в исходном ТЗ.

**`ONIMBOTV2MESSAGEADD`** — тоже пришло **form-urlencoded** с
bracket-нотацией (`data[message][text]` и т.п.), а не JSON, как
предполагалось изначально; `domain`/`application_token` вложены в
`data[bot][auth][...]`; поле автора сообщения реально называется `authorId`
(camelCase) — предположение подтвердилось.

## 1.5 Чат-боты — API v2, не v1

- Регистрация: `imbot.v2.Bot.register` (не `imbot.register`).
- Отправка: `imbot.v2.Chat.Message.send` (не `imbot.message.add`).
- Передача оператору: `imopenlines.bot.session.operator` (пока не проверено
  на реальном портале, параметры в коде — предположение).
- **Маршрутизация входящих — по `bot.id` из события**, не циклом по
  `enabled_modules` клиента: у каждого модуля свой отдельный бот —
  структурная гарантия, что на одно сообщение ответит ровно один модуль.

## 1.6 Очередь задач (Celery)

- Заложен полноценный Celery + Redis (с прицелом на масштаб и переиспользование
  вне Bitrix — напр. личные крипто-парсеры).
- **Текущая реальность (Render Free, без Background Worker):**
  `CELERY_TASK_ALWAYS_EAGER=True` — задачи выполняются синхронно в веб-процессе,
  `REDIS_URL` не обязателен (есть дефолт).
- `core/async_utils.run_async()` — обёртка вместо голого `asyncio.run()`:
  определяет, есть ли уже работающий event loop, и корректно выполняет
  корутину в обоих случаях.
- Найденная и исправленная проблема: один глобальный async `engine` на процесс
  не работал в eager-режиме (поток с отдельным event loop) — `asyncpg`
  соединения из чужого loop падали с `attached to a different loop`. Решение:
  `engine` теперь **thread-local** (`core/db.py`) + явный `dispose()` в конце
  потока (`core/async_utils.py`).
- На будущее (платный тариф с отдельным воркером) — просто убрать
  `CELERY_TASK_ALWAYS_EAGER`, код менять не придётся.

## 1.7 Миграции (Alembic)

- Схема управляется **только** через Alembic, никакого `Base.metadata.create_all()`.
- `alembic.ini`: `script_location = %(here)s/alembic` — путь от расположения
  самого `alembic.ini`.
- `alembic/env.py` сам создаёт `CREATE EXTENSION IF NOT EXISTS vector`
  (для pgvector).
- **Важно:** после `connection.run_sync(do_run_migrations)` нужен явный
  `await connection.commit()` — без него транзакция может откатиться при
  закрытии соединения без явной ошибки (реальный баг: таблицы "не
  создавались" несмотря на чистый прогон миграции).
- `script.py.mako` должен содержать `import pgvector.sqlalchemy` —
  автогенерация Alembic не добавляет импорты для кастомных типов колонок сама.
- Прогон миграций — локально, через External Database URL от Render, с
  ручной заменой схемы на `postgresql+asyncpg://` (Render отдаёт голый
  `postgresql://`); Pre-Deploy Command / Shell на Free-тарифе нет.

## 1.8 Деплой (Render, Free-тариф)

- Один `Dockerfile`, один Web Service (Free-тариф не даёт Background Worker).
- Обязательные env-переменные (`config.py` падает при старте без них):
  `DATABASE_URL`, `BITRIX_CLIENT_ID`, `BITRIX_CLIENT_SECRET`,
  `PUBLIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ADMIN_API_KEY`.
  - `ANTHROPIC_API_KEY` сделан по-настоящему опциональным на старте (ленивая
    инициализация клиента) — сервис поднимается и без него, падает только при
    реальном обращении к AI-модулю без ключа.
- `REDIS_URL` — опционален, есть дефолт.
- `python-dotenv` подключён в `config.py` — переменные из `.env` подхватываются
  локально, если Windows-shell их не проставил надёжно.
- Сервис: `https://ai-core-7099.onrender.com`.
- Обычный цикл правки кода: файл → `git add`/`commit`/`push` → Render
  передеплоивает сам; миграции и разовые скрипты — отдельно, вручную, с
  локальной машины (`D:\Projects\bitrix-ai-core\project`, venv активирован),
  через внешний `DATABASE_URL` (Pre-Deploy Command не используется).

### Шпаргалка (рутинные операции)
```powershell
$env:DATABASE_URL = "postgresql+asyncpg://...внешний хост Render..."
$env:PUBLIC_BASE_URL = "https://ai-core-7099.onrender.com"
```
- Применить миграции: `alembic upgrade head`
- Активировать модуль клиенту:
  ```powershell
  python -c "import asyncio; from adapters.bitrix.auth import activate_module; asyncio.run(activate_module('<member_id>', '<module_name>'))"
  ```
- Посмотреть поля CRM-сущности (нужен `entityTypeId`):
  ```powershell
  python -c "
  import asyncio
  from adapters.bitrix.bitrix_client import call_bitrix_method
  from core.db import get_session
  from core.models import Client
  from sqlalchemy import select

  async def main():
      async with get_session() as session:
          result = await session.execute(select(Client).where(Client.member_id == '<member_id>'))
          client = result.scalar_one()
          response = await call_bitrix_method(client, 'crm.item.fields', {'entityTypeId': <id>})
          print(response)

  asyncio.run(main())
  "
  ```

## 1.9 Известные технические долги ядра

- **Порядок операций** "сначала сделка в Bitrix CRM, потом коммит локальной
  сессии" — риск осиротевших сделок при частичном сбое (это же всплыло и в
  модуле seller, см. §3.6). Правильный порядок: сначала локальная сессия
  (напр. статус `pending`), потом Bitrix. Не исправлено, задача перед
  реальным продакшеном.
- `/debug/db` в `main.py` — временный диагностический эндпоинт (публично
  показывает структуру подключения к БД), **обязательно убрать** перед
  реальным продакшеном. Пока оставлен, не мешает.
- Если в БД уже есть данные в старом формате `bot_ids = {"module": <int>}`
  (до миграции на v2) — нужна отдельная миграция данных (актуально, только
  если такие данные реально существуют у клиентов).
- `adapters/telegram/`, `adapters/web_parser/` (кроме уже сделанного
  Telegram-адаптера seller-модуля) — будущие каналы, не созданы.

---

# 2. МОДУЛЬ «TESTING» (аттестация менеджеров через Bitrix-бота)

Полностью проверен end-to-end на тестовом портале.

## 2.1 Подтверждено рабочим

1. Установка приложения (`/oauth/install`) — реальный формат события
   подтверждён.
2. Регистрация бота (`activate_module` → `imbot.v2.Bot.register`) — бот
   `AI testing` реально создаётся в Bitrix, находится в мессенджере портала.
3. Приём сообщений (`/bot/handler`, `ONIMBOTV2MESSAGEADD`) — парсится и
   обрабатывается верно.
4. Полный цикл модуля: создание `TestSession`, пошаговые вопросы, подсчёт
   баллов, сообщение с итогом, запись результата в CRM смарт-процесс —
   проверено вживую в чате Bitrix.
5. Запись результата в CRM (`_report_result_to_crm`) — запись создаётся, оба
   кастомных поля заполняются корректно.

## 2.2 Специфичная находка модуля

- Программный код поля для `crm.item.add`/`update` (новые методы
  `crm.item.*`) — `ufCrm7_...` (ключ словаря из `crm.item.fields`), **а не**
  `UF_CRM_7_...` (upperName). С upperName запись создавалась, но кастомные
  поля молча оставались пустыми.

## 2.3 Конфигурация тестового клиента/портала

Домен: `b24-l4y6ak.bitrix24.by`, `member_id`: `81969d52b9813de2b5c11a01f019a2f6`

- CRM смарт-процесс «Аттестация менеджеров»: `entityTypeId = 1038`.
- Поле «Результат теста» (число): `ufCrm7_1787776021255`.
- Поле «Статус аттестации» (список): `ufCrm7_1787776088286`
  - `45` — Пройдена
  - `47` — Не пройдена
  - `49` — Требуется пересдача (**пока не используется** — нет критерия,
    когда его назначать).
- Критерий «Пройдена» сейчас: все ответы верны (`score == total`). Порог не
  настраивался.
- `crm.item.add` создаёт **новую запись** на каждое прохождение теста, не
  ищет и не обновляет существующую по пользователю.

## 2.4 Что осталось заглушкой

1. **`generate_questions(domain)`** — сейчас 2 захардкоженных вопроса. В
   целевом виде: генерация через Claude на основе Базы Знаний клиента
   (`knowledge_chunks`, pgvector — инфраструктура уже готова и используется
   модулем `seller`). Не начато — не решено, откуда и как загружать тексты БЗ
   конкретного клиента (в README упоминается
   `tasks.ingest_tasks.ingest_kb_text()`, файл ещё не смотрели в этом
   контексте).
2. `imopenlines.bot.session.operator`/`.transfer`/`.finish` (эскалация на
   оператора) — не проверено на реальном портале (относится и к ядру, §1.5).

---

# 3. МОДУЛЬ «SELLER» + TELEGRAM-АДАПТЕР (AI-продавец, RAG, воронка)

Чек-пойнт на момент, когда Anthropic ещё не оплачен (заработает в
воскресенье) — весь остальной путь (до вызова Claude) протестирован и
работает.

## 3.1 Модели (`core/models.py`)

- **`SellerPipeline`** — одно направление продаж = один Telegram-бот = своя
  связка каталог/КБ/воронка. Поля: `token`, `bitrix_catalog_ids` (список — одно
  направление может объединять несколько каталогов Bitrix, например бот
  «гаджеты» = ноутбуки+телефоны+комплектующие), `bitrix_category_id`,
  `bitrix_line_id`, `field_schema` (конфигурируемый набор полей для сбора у
  клиента — товары и услуги требуют разных полей), `stages` (конфигурируемый
  список стадий воронки — у разных направлений разные стадии).
- **`SellerSession`** — состояние диалога с одним клиентом Telegram:
  `collected_fields`, `current_stage_key`, `telegram_chat_id` (BigInteger — см.
  баг ниже), `bitrix_chat_id`/`dialog_id`/`deal_id`, `last_bot_message_id`,
  `confirmation_shown`.
- `KnowledgeChunk` пересобран с `member_id` на `pipeline_id`.

## 3.2 Миграции (актуальный head — `b1e6a4c8d2f0`)
```
808f53c325b5 (init)
-> 3f1c9a2b7d4e (seller pipeline + sessions, knowledge_chunks -> pipeline_id)
-> 5a8e2d1f9c3b (SellerPipeline.bitrix_catalog_ids)
-> 9c4f7e2a1b6d (SellerSession.confirmation_shown)
-> b1e6a4c8d2f0 (SellerSession.telegram_chat_id: Integer -> BigInteger)
```

## 3.3 Код

- **`tasks/seller_tasks.py`** — отдельный Celery-таск
  `process_seller_message(pipeline_id, payload)`, роутится по `pipeline`, не
  по `member_id+module_name` (как `tasks/dispatch.py` для Bitrix-модулей).
  После `seller_service.handle()` реально доставляет `AgentReply` в Telegram
  (`sendMessage` + `answerCallbackQuery` на нажатие кнопки).
- **`tasks/ingest_tasks.py`** — синхронизация каталога Bitrix в
  `knowledge_chunks`, скоуп по `pipeline.bitrix_catalog_ids`.
- **`adapters/telegram/`** (новый пакет, `adapters/bitrix/` не тронут):
  - `bot_handler.py` — вебхук `POST /telegram/webhook/{bot_token}`, находит
    pipeline по токену, обрабатывает обычные сообщения и `callback_query`
    (нажатия инлайн-кнопок).
  - `telegram_client.py` — обёртка над Telegram Bot API (`send_message`,
    `edit_message_text`, `answer_callback_query`).
  - `admin.py` — защищённый эндпоинт `POST /telegram/admin/pipelines`,
    создаёт `SellerPipeline` + вызывает `setWebhook` одним запросом; защита —
    заголовок `X-Admin-Key` против env `ADMIN_API_KEY`.
- **`services/seller_service.py`** — полный пайплайн: RAG по
  `knowledge_chunks` (локальные sentence-transformers эмбеддинги без внешнего
  API, pgvector для similarity search), один вызов Claude с tool use
  (`update_deal`) для заполнения полей сделки и смены стадии воронки,
  эскалация оператору при `ESCALATION_MARKER` (структурный маркер, не парсинг
  текста), детерминированная кнопка «Подтвердить заказ» (не решение модели —
  показывается один раз, когда все `required`-поля из `field_schema»
  собраны); `_confirm_order` переводит сделку на последнюю стадию из
  `pipeline.stages`.

## 3.4 Живой тест на проде

- Сервис: `https://ai-core-7099.onrender.com`.
- Тестовый клиент: `member_id=81969d52b9813de2b5c11a01f019a2f6`
  (`b24-l4y6ak.bitrix24.by`).
- `SellerPipeline #1` создан через `/telegram/admin/pipelines`, тестовая
  схема (для проверки механики, не реальных полей CRM):
  ```json
  "field_schema": [
    {"key": "product", "label": "Что хочет купить", "required": true, "bitrix_field": null},
    {"key": "phone", "label": "Телефон для связи", "required": true, "bitrix_field": null}
  ],
  "stages": [
    {"key": "new", "bitrix_stage_id": "NEW", "description": "Первое обращение"},
    {"key": "confirmed", "bitrix_stage_id": "WON", "description": "Клиент подтвердил заказ"}
  ]
  ```
- Реальный Telegram-бот создан через @BotFather, `setWebhook` подтверждён
  (`ok: true`).
- Подтверждено рабочим: OAuth-рефреш токена, `crm.deal.add` (сделка реально
  создаётся в Bitrix), создание `SellerSession` в БД (после фикса
  BigInteger).
- **Ещё не проверено**: сам ответ AI клиенту (упирается в неоплаченный
  Anthropic), кнопка подтверждения (требует, чтобы AI сначала собрал оба
  поля), синхронизация диалога в Bitrix Open Lines (imconnector —
  сознательно отложено, см. §3.5).

## 3.5 Осознанно отложено (не баги, а решения)

- **imconnector-мост Telegram ↔ Bitrix Open Lines** — не реализован. Разрыв
  принят осознанно («тестируем пока создание сущностей в CRM»), вернуться
  после проверки основного AI-диалога (детали моста —
  `imconnector.register/activate/connector.data.set/send.messages` и событие
  `OnImConnectorMessageAdd` — уже обсуждались отдельно).
- `field_schema`/`stages` тестового pipeline упрощённые — реальные поля CRM
  (`bitrix_field`) везде `null`, `bitrix_category_id=0`, `bitrix_line_id=1` —
  заглушки для проверки механики, не финальная конфигурация.

## 3.6 Найденные и исправленные баги

1. **Int32 overflow на `telegram_chat_id`** — современные Telegram-аккаунты
   дают ID больше `2^31-1` (пример из логов: `8658993738`). Колонка была
   `Integer`, стала `BigInteger` (миграция `b1e6a4c8d2f0`).
2. **Дублирующиеся сделки в CRM** — пока была ошибка выше, `crm.deal.add`
   успевал отработать до падения на сохранении `SellerSession`; Telegram
   ретраил недоставленный вебхук, каждый ретрай создавал новую сделку. После
   фикса типа колонки проблема ушла сама (первопричина та же, что и
   архитектурный риск в §1.9/§3.5 ниже — порядок операций).
3. **`tasks/seller_tasks.py` изначально не доставлял ответ AI в Telegram** —
   таск вызывал `seller_service.handle()`, но результат никуда не
   отправлялся. Исправлено — теперь вызывает `telegram_client.send_message`.

## 3.7 Технический долг модуля

- Порядок операций в `_get_or_create_session`: сейчас `crm.deal.add`
  вызывается ДО коммита `SellerSession` в БД. Риск — «сирота» в CRM без
  записи в БД при ошибке между шагами. Правильно — сначала резервировать
  `SellerSession` (статус `pending`), потом идти в Bitrix. Не критично для
  тестов, обязательно поправить перед реальной нагрузкой (тот же паттерн,
  что зафиксирован для ядра в целом, §1.9).

## 3.8 План на воскресенье (когда заработает Anthropic)

1. Написать боту в Telegram тестовое сообщение с обоими полями сразу
   (например: «хочу телефон, мой номер +79991234567») или по очереди.
2. Проверить в логах Render, что Claude отвечает, `update_deal` вызывается,
   `collected_fields` в `SellerSession` заполняется.
3. Проверить, что после сбора обоих полей появляется кнопка «Подтвердить
   заказ», а нажатие переводит сделку на стадию `WON` и завершает сессию.
4. Дальше — imconnector-мост с Open Lines.
