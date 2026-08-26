"""
Точка входа FastAPI.

Намеренно НЕ вызывает Base.metadata.create_all() — схема БД управляется
исключительно через Alembic-миграции (alembic upgrade head), это позволяет
безопасно эволюционировать схему (новые поля, индексы) без потери контроля
над продакшен-базой.
"""
import logging
from urllib.parse import urlparse

from fastapi import FastAPI
from sqlalchemy import text

from adapters.bitrix.auth import router as bitrix_auth_router
from adapters.bitrix.bot_handler import router as bitrix_bot_router
from config import DATABASE_URL
from core.db import get_session

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Multi-tenant Bitrix24 AI core")

app.include_router(bitrix_auth_router)
app.include_router(bitrix_bot_router)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/debug/db")
async def debug_db():
    """
    ВРЕМЕННЫЙ диагностический эндпоинт — убрать перед реальным продакшеном.
    Не просто читает DATABASE_URL из конфига, а делает РЕАЛЬНЫЙ запрос через
    тот же core.db.get_session(), которым пользуется /oauth/install — чтобы
    исключить любые сомнения в духе "а вдруг это разные пути подключения".
    """
    parsed = urlparse(DATABASE_URL)
    tables = []
    error = None
    try:
        async with get_session() as session:
            result = await session.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
            tables = [row[0] for row in result.fetchall()]
    except Exception as exc:  # noqa: BLE001 — это диагностика, нужен любой текст ошибки как есть
        error = repr(exc)
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "database": parsed.path.lstrip("/"),
        "user": parsed.username,
        "tables_visible_to_app": tables,
        "query_error": error,
    }
