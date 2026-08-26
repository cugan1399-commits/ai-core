"""
Общий слой доступа к БД. Не знает ничего про Bitrix, Telegram или конкретные фичи —
им пользуются все модули проекта одинаково.

Важно: схема БД управляется ТОЛЬКО через Alembic-миграции.
Здесь нет Base.metadata.create_all() — это осознанное решение, см. README.

ПОЧЕМУ engine THREAD-LOCAL, А НЕ ГЛОБАЛЬНЫЙ (найдено при отладке 2026-08-26):
asyncpg-соединения жёстко привязаны к тому event loop, в котором были открыты —
их нельзя использовать из другого loop. Раньше engine создавался один раз при
импорте модуля и был привязан к главному loop'у uvicorn. Но core/async_utils.py
в eager-режиме Celery (CELERY_TASK_ALWAYS_EAGER=True, временный режим для
Free-тарифа без отдельного Background Worker — см. config.py) запускает
обработку в ОТДЕЛЬНОМ потоке со своим НОВЫМ loop, дёрнутом прямо из
async-обработчика FastAPI. Использование оттуда старого engine приводило к
"got Future <Future pending> attached to a different loop" — как на реальных
запросах, так и при попытке закрыть простаивающие соединения из предыдущего
вызова уже после того, как их loop завершился вместе с потоком.

Решение: у каждого потока — свой engine, создаваемый лениво при первом
обращении в этом потоке (значит, и в правильном loop'е). Для главного потока
FastAPI/uvicorn это не меняет поведения (engine создаётся один раз и живёт всё
время работы процесса, как и раньше). Для eager-потоков — engine создаётся
внутри их собственного asyncio.run() и должен быть явно закрыт
(dispose_current_thread_engine) ДО завершения потока — это делает
core/async_utils.run_async.
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL

_local = threading.local()


def _get_sessionmaker():
    if getattr(_local, "engine", None) is None:
        engine: AsyncEngine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
        _local.engine = engine
        _local.sessionmaker = async_sessionmaker(
            bind=engine,
            expire_on_commit=False,
            autoflush=False,
        )
    return _local.sessionmaker


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
    session = _get_sessionmaker()()
    try:
        yield session
    finally:
        await session.close()


async def dispose_current_thread_engine() -> None:
    """
    Закрывает engine (и пул соединений) ТЕКУЩЕГО потока. Обязательно вызывать
    перед завершением потока/event loop, в котором этот engine был создан —
    см. core/async_utils.run_async. Без этого соединения пытаются закрыться
    уже после смерти их loop'а, что приводит к тем же ошибкам
    "attached to a different loop", только при сборке мусора, а не при запросе.

    Безопасно вызывать, даже если engine в этом потоке не создавался — тогда
    просто ничего не делает.
    """
    engine = getattr(_local, "engine", None)
    if engine is not None:
        await engine.dispose()
        _local.engine = None
        _local.sessionmaker = None
