"""
Админ-эндпоинт для подключения нового Telegram-бота (SellerPipeline).

Вызывается вручную (curl/Postman) при подключении нового направления продаж
клиенту. Делает две вещи атомарно по смыслу (не по транзакции — Telegram
снаружи БД, как и Bitrix в остальном проекте):
1. Создаёт строку SellerPipeline.
2. Регистрирует вебхук в Telegram (setWebhook) на этот бот.

ВАЖНО: эндпоинт ничем не защищён (нет auth) — открывать наружу нельзя как есть.
Для MVP предполагается вызов только с доверенной машины/через VPN, либо
временно закрыт файрволом. Добавить хотя бы простой API-key header перед тем,
как эндпоинт станет доступен кому-то кроме вас.
"""
from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from config import ADMIN_API_KEY, PUBLIC_BASE_URL
from core.db import get_session
from core.models import Client, SellerPipeline

def verify_admin_key(x_admin_key: str = Header(...)) -> None:
    if x_admin_key != ADMIN_API_KEY:
        raise HTTPException(401, "Неверный или отсутствующий X-Admin-Key")

router = APIRouter(
    prefix="/telegram/admin",
    tags=["telegram-admin"],
    dependencies=[Depends(verify_admin_key)],
)


class FieldSchemaItem(BaseModel):
    key: str
    label: str
    required: bool = False
    bitrix_field: str | None = None


class StageItem(BaseModel):
    key: str
    bitrix_stage_id: str
    description: str


class CreatePipelineRequest(BaseModel):
    member_id: str
    name: str
    telegram_bot_token: str
    bitrix_category_id: int
    bitrix_line_id: int
    bitrix_catalog_ids: list[int] = Field(default_factory=list)
    field_schema: list[FieldSchemaItem] = Field(default_factory=list)
    stages: list[StageItem]  # обязателен хотя бы один — первый становится дефолтной стадией


@router.post("/pipelines")
async def create_pipeline(body: CreatePipelineRequest):
    if not body.stages:
        raise HTTPException(400, "stages не может быть пустым — нужна хотя бы одна стадия")

    async with get_session() as session:
        client_result = await session.execute(
            select(Client).where(Client.member_id == body.member_id, Client.is_active.is_(True))
        )
        if client_result.scalar_one_or_none() is None:
            raise HTTPException(404, f"Клиент {body.member_id} не найден или не активен")

        pipeline = SellerPipeline(
            member_id=body.member_id,
            name=body.name,
            telegram_bot_token=body.telegram_bot_token,
            bitrix_category_id=body.bitrix_category_id,
            bitrix_line_id=body.bitrix_line_id,
            bitrix_catalog_ids=body.bitrix_catalog_ids,
            field_schema=[f.model_dump() for f in body.field_schema],
            stages=[s.model_dump() for s in body.stages],
        )
        session.add(pipeline)
        await session.commit()
        await session.refresh(pipeline)

    webhook_url = f"{PUBLIC_BASE_URL}/telegram/webhook/{body.telegram_bot_token}"
    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.post(
            f"https://api.telegram.org/bot{body.telegram_bot_token}/setWebhook",
            json={"url": webhook_url},
        )
    telegram_result = response.json()

    if not telegram_result.get("ok"):
        # Pipeline уже создан в БД, но Telegram отказался ставить вебхук (неверный
        # токен, например) — не откатываем создание, а явно сообщаем о проблеме,
        # чтобы можно было починить токен и повторить setWebhook отдельно.
        raise HTTPException(
            502,
            f"SellerPipeline {pipeline.id} создан, но setWebhook не прошёл: {telegram_result}",
        )

    return {
        "pipeline_id": pipeline.id,
        "webhook_url": webhook_url,
        "telegram_response": telegram_result,
    }