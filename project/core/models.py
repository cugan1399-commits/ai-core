"""
Модели данных.

Ключевые решения, зафиксированные в обсуждении архитектуры:
- enabled_modules — список модулей, валидируется по ALLOWED_MODULES, дефолт [] (никогда не
  включается автоматически).
- bot_ids — на КАЖДЫЙ активный модуль клиента регистрируется отдельный imbot (API v2:
  imbot.v2.Bot.register) в Bitrix24 (это требование самого Bitrix API — бот привязан
  к порталу). Хранит и числовой id, и botToken — оба нужны для методов imopenlines.bot.session.*
  (передача диалога оператору требует тот же CLIENT_ID/botToken, что был при регистрации).
  bot_handler маршрутизирует входящее сообщение по bot.id из события, а не циклом по всем
  enabled_modules — иначе при нескольких активных модулях у одного клиента ответили бы сразу
  два сервиса на одно сообщение.
- Токены OAuth обновляются через optimistic CAS (см. adapters/bitrix/bitrix_client.py),
  поэтому здесь нет полей под блокировки — конкурентность решается на уровне UPDATE ... WHERE.

ПРИМЕЧАНИЕ (миграция v1 → v2): изначально bot_ids хранил только числовой BOT_ID (imbot.register,
API v1). Bitrix24 пометил imbot.* как устаревший в пользу imbot.v2.* — обновлено здесь и в
adapters/bitrix/. Если в БД уже есть данные в старом формате {"module": <int>}, потребуется
миграция данных (не только схемы) при апгрейде существующей установки.

ПРИМЕЧАНИЕ (seller: pipeline вместо привязки к client напрямую): один клиент (Bitrix-портал)
может продавать несколько разных направлений (например, автозапчасти и пироги) — у каждого
свой Telegram-бот, свой каталог, свои поля сделки и своя воронка. Поэтому SellerPipeline —
это отдельная сущность "одно направление продаж", а не просто настройка внутри Client.
KnowledgeChunk скоупится по pipeline_id (а не member_id), чтобы каталог одного направления
не подмешивался в поиск другого направления того же клиента.
"""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from config import ALLOWED_MODULES, EMBEDDING_DIMENSIONS
from core.db import Base


class Client(Base):
    """Один портал Bitrix24, установивший приложение."""

    __tablename__ = "clients"

    member_id: Mapped[str] = mapped_column(String, primary_key=True)
    # unique=True: TestSession ссылается на domain как на внешний ключ (ниже) —
    # Postgres требует, чтобы столбец на другом конце FOREIGN KEY был уникальным.
    # По факту у каждого портала Bitrix ровно один домен, так что это честное
    # ограничение, а не искусственное.
    domain: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    access_token: Mapped[str] = mapped_column(String, nullable=False)
    refresh_token: Mapped[str] = mapped_column(String, nullable=False)
    application_token: Mapped[str] = mapped_column(String, nullable=False)

    # Какие фичи включены клиенту. Список строк, каждая обязана быть в ALLOWED_MODULES.
    # Дефолт — пустой список: активация всегда осознанное отдельное действие.
    enabled_modules: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)

    # Данные бота Bitrix для каждого активного модуля:
    # {"testing": {"id": 148, "token": "..."}, "seller": {"id": 152, "token": "..."}}.
    # "id" — bot.id из ответа imbot.v2.Bot.register, используется для маршрутизации
    # входящих событий и для imbot.v2.Chat.Message.send.
    # "token" — botToken, переданный при регистрации; обязателен для imopenlines.bot.session.transfer
    # и imopenlines.bot.session.finish (Bitrix требует тот же CLIENT_ID/botToken).
    bot_ids: Mapped[dict[str, dict]] = mapped_column(JSON, nullable=False, default=dict)

    is_active: Mapped[bool] = mapped_column(default=True)  # False после ONAPPUNINSTALL

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @validates("enabled_modules")
    def _validate_enabled_modules(self, key: str, value: list[str]) -> list[str]:
        unknown = set(value) - ALLOWED_MODULES
        if unknown:
            raise ValueError(f"Неизвестные модули в enabled_modules: {unknown}")
        return value


class TestSession(Base):
    """Пошаговая сессия тестирования менеджера (state machine модуля 'testing')."""

    __tablename__ = "test_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String, ForeignKey("clients.domain"), nullable=False)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)  # ID сотрудника в Б24

    current_step: Mapped[int] = mapped_column(Integer, default=0)
    current_score: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String, default="active")  # 'active' | 'completed'

    # Массив сгенерированных вопросов и эталонных ответов:
    # [{"question": "...", "answer": "..."}, ...]
    questions_json: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("status in ('active', 'completed')", name="ck_test_sessions_status"),
        # Частичный уникальный индекс: у одного (domain, user_id) не может быть
        # больше одной АКТИВНОЙ сессии одновременно. Защищает от дублей при
        # повторных/гоночных вебхуках от Bitrix.
        Index(
            "ux_test_sessions_active_per_user",
            "domain",
            "user_id",
            unique=True,
            postgresql_where=(status == "active"),  # type: ignore[arg-type]
        ),
    )


class SellerPipeline(Base):
    """
    Одно направление продаж = один Telegram-бот = свой каталог/КБ/воронка.
    Один клиент (Bitrix-портал) может иметь несколько pipeline одновременно
    (например, "Автозапчасти" и "Пироги") — каждый работает через свой
    Telegram bot token, свою Открытую линию (imconnector) и свой набор
    knowledge_chunks. seller_service.py при этом один и тот же код для всех
    pipeline — разница только в данных этой таблицы.
    """

    __tablename__ = "seller_pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str] = mapped_column(String, ForeignKey("clients.member_id"), nullable=False)

    name: Mapped[str] = mapped_column(String, nullable=False)  # "Автозапчасти", "Пироги" — для админки/логов

    # Отдельный токен на каждый Telegram-бот. Адаптер определяет pipeline_id
    # по тому, на какой вебхук/токен пришёл апдейт — до всякого обращения к AI.
    telegram_bot_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    bitrix_category_id: Mapped[int] = mapped_column(Integer, nullable=False)  # направление сделки в CRM
    bitrix_line_id: Mapped[int] = mapped_column(Integer, nullable=False)      # Открытая линия под imconnector

    # Список ID каталогов Bitrix (товарных каталогов CRM), которые относятся
    # к этой сфере. Один pipeline может объединять несколько каталогов
    # (например, "Ноутбуки" + "Телефоны" + "Железо" — одна сфера, один бот,
    # одна воронка, но три каталога синхронизируются в его knowledge_chunks).
    # Используется в tasks/ingest_tasks.py при синхронизации.
    bitrix_catalog_ids: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)

    # Схема полей для сбора у клиента, специфичная для этого направления.
    # [{"key": "quantity", "label": "Количество", "required": true, "bitrix_field": "UF_CRM_..."}, ...]
    field_schema: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)

    # Стадии воронки этого направления, в порядке прохождения.
    # [{"key": "new", "bitrix_stage_id": "NEW", "description": "..."}, ...]
    # Первый элемент списка — стадия по умолчанию для новой SellerSession.
    # ПОСЛЕДНИЙ элемент — финальная/успешная стадия (см. seller_service._confirm_order).
    stages: Mapped[list[dict]] = mapped_column(JSON, nullable=False, default=list)

    is_active: Mapped[bool] = mapped_column(default=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class SellerSession(Base):
    """
    Состояние диалога с одним клиентом Telegram в рамках одного SellerPipeline.
    Аналог TestSession, но для модуля 'seller': здесь копится карточка будущей
    сделки (collected_fields) и текущая стадия воронки, вместо вопросов теста.

    Сделка в CRM создаётся сразу при первом сообщении клиента (пустая/минимальная
    карточка, дозаполняется по ходу диалога) — так менеджер видит обращение сразу,
    а не только когда AI соберёт все обязательные поля.
    """

    __tablename__ = "seller_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("seller_pipelines.id"), nullable=False)

        telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Bitrix-стороние идентификаторы, полученные от imconnector.send.messages
    # при первом сообщении клиента. None, пока чат ещё не создан в Bitrix.
    bitrix_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bitrix_dialog_id: Mapped[str | None] = mapped_column(String, nullable=True)

    # ID сделки в CRM. Создаётся сразу при первом сообщении клиента (см. докстринг класса).
    bitrix_deal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    collected_fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    current_stage_key: Mapped[str] = mapped_column(String, nullable=False)

    # id последнего сообщения бота в Telegram — нужен адаптеру, чтобы решать
    # редактировать это сообщение (editMessageText) или отправлять новое.
    last_bot_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Показана ли клиенту кнопка подтверждения заказа. Нужен отдельный флаг,
    # а не просто "все required-поля собраны" — иначе кнопка будет всплывать
    # заново в КАЖДОМ ответе после того, как поля уже собраны.
    confirmation_shown: Mapped[bool] = mapped_column(default=False)

    status: Mapped[str] = mapped_column(String, default="active")  # 'active' | 'completed' | 'escalated'

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status in ('active', 'completed', 'escalated')", name="ck_seller_sessions_status"
        ),
        # Один активный диалог на клиента в рамках одного pipeline — защита от
        # дублей при повторных/гоночных апдейтах Telegram, как у TestSession.
        Index(
            "ux_seller_sessions_active_per_chat",
            "pipeline_id",
            "telegram_chat_id",
            unique=True,
            postgresql_where=(status == "active"),  # type: ignore[arg-type]
        ),
    )


class KnowledgeChunk(Base):
    """
    Единое хранилище знаний модуля 'seller' — и каталог, и База Знаний превращаются
    в чанки текста с эмбеддингом и лежат в одной таблице. Это даёт один векторный
    поиск сразу по обоим источникам, вместо двух параллельных систем поиска.

    source_type различает происхождение чанка:
    - 'catalog' — синтезировано из карточки товара/услуги Bitrix CRM (см. tasks/ingest_tasks.py)
    - 'kb'      — загружено вручную как текст Базы Знаний

    source_id — исходный идентификатор (ID товара в CRM, или ID документа КБ) — нужен,
    чтобы при повторной синхронизации каталога обновлять существующий чанк, а не плодить дубли.

    ИЗМЕНЕНИЕ: раньше скоупилось по member_id, теперь по pipeline_id — потому что
    у одного клиента каталог различается между направлениями продаж (см. SellerPipeline),
    а не только между разными клиентами.
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("seller_pipelines.id"), nullable=False)

    source_type: Mapped[str] = mapped_column(String, nullable=False)  # 'catalog' | 'kb'
    source_id: Mapped[str] = mapped_column(String, nullable=False)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSIONS), nullable=False)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("source_type in ('catalog', 'kb')", name="ck_knowledge_chunks_source_type"),
        # Повторная синхронизация одного и того же товара/документа обновляет
        # существующую строку (upsert по этому ключу), а не создаёт дубль.
        Index(
            "ux_knowledge_chunks_source",
            "pipeline_id",
            "source_type",
            "source_id",
            unique=True,
        ),
    )
