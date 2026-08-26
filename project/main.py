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

from adapters.bitrix.auth import router as bitrix_auth_router
from adapters.bitrix.bot_handler import router as bitrix_bot_router
from config import DATABASE_URL

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
    ВРЕМЕННЫЙ диагностический эндпоинт — убрать перед реальным продакшеном
    с настоящими клиентами (не должен быть публично доступен на боевой системе).
    Показывает, к какому хосту/базе реально подключён работающий контейнер,
    без пароля — чтобы свериться с тем, что видно в дашборде Render.
    """
    parsed = urlparse(DATABASE_URL)
    return {"host": parsed.hostname, "port": parsed.port, "database": parsed.path.lstrip("/"), "user": parsed.username}
