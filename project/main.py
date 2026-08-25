"""
Точка входа FastAPI.

Намеренно НЕ вызывает Base.metadata.create_all() — схема БД управляется
исключительно через Alembic-миграции (alembic upgrade head), это позволяет
безопасно эволюционировать схему (новые поля, индексы) без потери контроля
над продакшен-базой.
"""
import logging

from fastapi import FastAPI

from adapters.bitrix.auth import router as bitrix_auth_router
from adapters.bitrix.bot_handler import router as bitrix_bot_router

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Multi-tenant Bitrix24 AI core")

app.include_router(bitrix_auth_router)
app.include_router(bitrix_bot_router)


@app.get("/health")
async def health():
    return {"status": "ok"}
