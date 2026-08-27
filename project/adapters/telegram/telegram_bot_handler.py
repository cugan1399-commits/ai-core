"""
Приём апдейтов от Telegram для модуля 'seller'.

Один физический эндпоинт обслуживает ВСЕ Telegram-боты (все SellerPipeline) —
конкретный pipeline определяется по токену в пути URL, который был передан
в Telegram при setWebhook(url=".../telegram/webhook/{bot_token}").

Как и adapters/bitrix/bot_handler.py, этот файл — ТОЛЬКО перевод "апдейт
Telegram" → "задача в очередь". Реальная доставка ответа клиенту (sendMessage/
editMessageText/answerCallbackQuery) происходит в tasks/seller_tasks.py, ПОСЛЕ
того как services/seller_service.py посчитает ответ — вебхук должен ответить
Telegram быстро (см. requirements Telegram на тайм-аут вебхука), поэтому не
ждём здесь результата AI.
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
    callback_query = update.get("callback_query")

    if message is not None:
        payload = {
            "telegram_chat_id": message["chat"]["id"],
            "telegram_user_id": message["from"]["id"],
            "text": message.get("text", ""),
            "message_id": message["message_id"],
            "callback_query_id": None,
        }
        process_seller_message.delay(pipeline.id, payload)

    elif callback_query is not None:
        # Нажатие инлайн-кнопки — превращаем в тот же формат payload, что и
        # обычное текстовое сообщение (seller_service не различает источник,
        # см. docstring handle() в services/seller_service.py). Дополнительно
        # прокидываем callback_query_id — он нужен tasks/seller_tasks.py, чтобы
        # снять "часики" с кнопки через answerCallbackQuery.
        chat = callback_query["message"]["chat"]
        payload = {
            "telegram_chat_id": chat["id"],
            "telegram_user_id": callback_query["from"]["id"],
            "text": callback_query["data"],  # например, "confirm_order"
            "message_id": callback_query["message"]["message_id"],
            "callback_query_id": callback_query["id"],
        }
        process_seller_message.delay(pipeline.id, payload)

    return {"ok": True}
