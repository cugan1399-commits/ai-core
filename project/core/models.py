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
"""
from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
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
    """

    __tablename__ = "knowledge_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    member_id: Mapped[str] = mapped_column(String, ForeignKey("clients.member_id"), nullable=False)

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
            "member_id",
            "source_type",
            "source_id",
            unique=True,
        ),
    )
