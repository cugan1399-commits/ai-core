# Multi-tenant Bitrix24 AI core

## Архитектура (кратко)

- **Одно приложение** в Bitrix24 (маркетплейс-тип) на всех клиентов — один `client_id`/`client_secret`.
- **`clients.enabled_modules`** — какие фичи включены клиенту (список, валидируется по `config.ALLOWED_MODULES`).
- **`clients.bot_ids`** — свой бот (imbot.v2, `{"id": int, "token": str}`) на каждый активный модуль клиента.
- **Роутинг по `bot.id`** из события, не циклом по модулям — гарантирует, что на одно сообщение ответит ровно один модуль.
- **`adapters/`** — перевод внешнего протокола (сейчас только Bitrix) во внутренний вызов задачи.
- **`services/`** — чистая бизнес-логика фичи, не знает о канале и не знает о других фичах.
- **`tasks/dispatch.py`** — мост между Bitrix-конкретикой и общей Celery-очередью.
- **`core/`** — общая инфраструктура (БД, очередь, эмбеддинги), не знает ни про Bitrix, ни про конкретные фичи.

## ⚠️ Миграция imbot v1 → v2

Bitrix24 пометил `imbot.*` как устаревший API. Весь код здесь использует актуальный `imbot.v2.*`:
- Регистрация бота: `imbot.v2.Bot.register` (не `imbot.register`)
- Событие: `ONIMBOTV2MESSAGEADD` (не `ONIMBOTMESSAGEADD`), JSON-тело, camelCase-поля
- Отправка сообщений: `imbot.v2.Chat.Message.send` (не `imbot.message.add`)
- Передача оператору: `imopenlines.bot.session.operator` / `.transfer` / `.finish`

**Требует проверки на тестовом портале перед продакшеном** (документация Bitrix не даёт
полной спецификации на момент написания):
- точное имя поля с id автора сообщения в событии (`data.message.authorId` — предположение)
- точные обязательные параметры `imopenlines.bot.session.operator` (`CHAT_ID`/`BOT_TOKEN` — предположение)

## Модуль "seller" — AI-агент (RAG)

- Каталог Bitrix (`crm.product.list`) и своя База Знаний хранятся в одной таблице
  `knowledge_chunks` (векторный поиск pgvector по обоим источникам сразу).
- Эмбеддинги — локальная модель `sentence-transformers` (`paraphrase-multilingual-MiniLM-L12-v2`),
  без внешнего API — экономия на масштабе (много клиентов × большие каталоги).
- Ответ генерирует Claude (`services/seller_service.py`), используя только найденные чанки как контекст.
- Если контекста недостаточно — модель возвращает структурированный маркер (не парсинг текста),
  и диалог передаётся живому оператору.
- Синхронизация каталога — периодическая Celery-задача `tasks.ingest_catalog_task`
  (нужно поставить в Celery beat, например раз в сутки). КБ — через `tasks.ingest_tasks.ingest_kb_text()`.

## Токены и конкурентность

Рефреш OAuth-токена — optimistic CAS (`UPDATE ... WHERE refresh_token = <старое значение>`),
без блокировки строки в БД — медленный Bitrix не блокирует другие вебхуки того же клиента.

## Схема БД

Управляется только через Alembic. Первая миграция сама создаёт расширение `pgvector`
(`CREATE EXTENSION IF NOT EXISTS vector`, см. `alembic/env.py`) — Postgres должен
поддерживать это расширение (доступно в managed Postgres большинства провайдеров,
либо `apt install postgresql-XX-pgvector` на VPS).

```bash
alembic revision --autogenerate -m "init"
alembic upgrade head
```

## Запуск

```bash
pip install -r requirements.txt
# sentence-transformers тянет за собой torch — установка займёт время и место на диске.

# переменные окружения:
# DATABASE_URL, REDIS_URL, BITRIX_CLIENT_ID, BITRIX_CLIENT_SECRET,
# PUBLIC_BASE_URL, ANTHROPIC_API_KEY

alembic upgrade head

# веб-процесс
uvicorn main:app --host 0.0.0.0 --port 8000

# воркер очереди (отдельный процесс)
celery -A core.queue.celery_app worker --loglevel=info

# периодическая синхронизация каталога (отдельный процесс)
celery -A core.queue.celery_app beat --loglevel=info
```

## Известные допущения / что нужно доделать перед реальным клиентом

- Поля события `ONIMBOTV2MESSAGEADD` и параметры `imopenlines.bot.session.operator` —
  свериться с реальным вызовом на тестовом портале (см. раздел про миграцию выше).
- `services/testing_service._report_result_to_crm` — заполнить реальный `entityTypeId`
  и ID полей смарт-процесса аттестации (конфигурация конкретного портала).
- `services/testing_service.generate_questions` — заменить заглушку на реальную генерацию
  вопросов по Базе Знаний.
- `adapters/telegram/`, `adapters/web_parser/` — будущие каналы, пока не созданы.
- Если в БД уже есть данные в старом формате `bot_ids = {"module": <int>}` (до миграции
  на v2) — нужна отдельная миграция данных, не только схемы.
