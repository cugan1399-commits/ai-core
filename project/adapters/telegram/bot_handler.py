"""
Приём апдейтов от Telegram для модуля 'seller'.

Один физический эндпоинт обслуживает ВСЕ Telegram-боты (все SellerPipeline) —
конкретный pipeline определяется по токену в пути URL, который был передан
в Telegram при setWebhook(url=".../telegram/webhook/{bot_token}"). Это и есть
разделение "какой бот принял сообщение", о которое просил пользователь —
без этого один и тот же вебхук-урл не мог бы отличить бота "Автозапчасти"
от бота "Пироги".

Как и adapters/bitrix/bot_handler.py, этот файл — ТОЛЬКО перевод "апдейт
Telegram" → "задача в очередь". Никакой бизнес-логики здесь нет.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import select

from core.db import get_session
from core.models import SellerPipeline
from tasks.seller_tasks import process_seller_message

router = APIRouter(prefix="/telegram", tags=["telegram"])


@router.post("/webhook/{bot_token}")
async def telegram_webhook(bot_token: str, request: Request):
    update = await request.json()

    async with get_session() as session:
        result = await session.execute(
            select(SellerPipeline).where(
                SellerPipeline.telegram_bot_token == bot_token,
                SellerPipeline.is_active.is_(True),
            )
        )
        pipeline = result.scalar_one_or_none()

    if pipeline is None:
        # Неизвестный/деактивированный токен — отвечаем 200, чтобы Telegram
        # не долбил ретраями бесконечно, но задачу не ставим.
        return {"ok": True}

    message = update.get("message") or update.get("edited_message")
    if message is None:
        # Пока обрабатываем только обычные сообщения; callback_query (кнопки)
        # подключим отдельно, когда добавим actions в AgentReply.
        return {"ok": True}

    payload = {
        "telegram_chat_id": message["chat"]["id"],
        "telegram_user_id": message["from"]["id"],
        "text": message.get("text", ""),
        "message_id": message["message_id"],
    }

    process_seller_message.delay(pipeline.id, payload)
    return {"ok": True}