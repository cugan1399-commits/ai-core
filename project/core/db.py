"""
Общий слой доступа к БД. Не знает ничего про Bitrix, Telegram или конкретные фичи —
им пользуются все модули проекта одинаково.

Важно: схема БД управляется ТОЛЬКО через Alembic-миграции.
Здесь нет Base.metadata.create_all() — это осознанное решение, см. README.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = async_sessionmaker(
    bind=engine,
    expire_on_commit=False,
    autoflush=False,
)


class Base(DeclarativeBase):
    """Общий declarative base для всех моделей проекта."""
    pass


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """
    Использование:
        async with get_session() as session:
            ...
    Гарантированно закрывает сессию, коммит/роллбек — на совести вызывающего кода.
    """
    session = SessionLocal()
    try:
        yield session
    finally:
        await session.close()
