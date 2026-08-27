"""
Диспетчер задач модуля 'seller', пришедших от Telegram-адаптера.

В отличие от tasks/dispatch.py (который роутит по member_id + module_name —
модель "один клиент, несколько модулей"), здесь роутинг идёт по pipeline_id
напрямую: один Telegram-бот = один pipeline, адаптер узнаёт pipeline_id уже
на входе (по тому, на какой webhook/токен пришёл апдейт), поэтому module_name
здесь просто не нужен.

Этот файл — единственное место, которое знает и про services/seller_service.py
(бизнес-логика), и про adapters/telegram/telegram_client.py (доставка ответа) —
он их связывает, сам не будучи ни тем, ни другим.
"""
from __future__ import annotations

from sqlalchemy import select

from adapters.telegram.telegram_client import answer_callback_query, send_message
from core.async_utils import run_async
from core.db import get_session
from core.models import SellerPipeline
from core.queue import celery_app
from services.seller_service import handle as handle_seller_message


@celery_app.task(name="tasks.process_seller_message", bind=True, max_retries=3, default_retry_delay=10)
def process_seller_message(self, pipeline_id: int, payload: dict) -> None:
    try:
        run_async(_process(pipeline_id, payload))
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc)


async def _process(pipeline_id: int, payload: dict) -> None:
    async with get_session() as session:
        result = await session.execute(
            select(SellerPipeline).where(SellerPipeline.id == pipeline_id)
        )
        pipeline = result.scalar_one_or_none()

    if pipeline is None or not pipeline.is_active:
        return  # pipeline отключён/удалён между постановкой задачи и её выполнением

    reply = await handle_seller_message(pipeline, payload)

    callback_query_id = payload.get("callback_query_id")
    if callback_query_id:
        # Обязательно снимаем "часики" с кнопки, даже если seller_service
        # ничего не ответил (например, эскалация оператору — reply is None).
        await answer_callback_query(pipeline.telegram_bot_token, callback_query_id)

    if reply is None:
        return  # эскалация оператору — клиенту в Telegram отдельно ничего не шлём

    await send_message(
        pipeline.telegram_bot_token,
        payload["telegram_chat_id"],
        reply.text,
        actions=reply.actions,
    )
