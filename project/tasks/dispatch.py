"""
Диспетчер задач, пришедших от Bitrix-адаптера.

Это единственный файл в tasks/, который знает про Bitrix (через Client-модель
и services.MODULE_HANDLERS). Будущие не-Bitrix модули (например, крипто-кошелёк
парсер) получат свой отдельный файл здесь же — tasks/wallet_tasks.py — который
не будет ничего импортировать из adapters/bitrix/ или знать про Client.
Общая для всех — только core/queue.py (сам Celery app).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from core.db import get_session
from core.models import Client
from core.queue import celery_app
from services import MODULE_HANDLERS


@celery_app.task(name="tasks.process_bot_message", bind=True, max_retries=3, default_retry_delay=10)
def process_bot_message(self, member_id: str, module_name: str, payload: dict) -> None:
    """
    Синхронная обёртка (Celery-таск) вокруг асинхронной обработки.
    Celery-воркер сам по себе синхронный, поэтому здесь единственный async-запуск
    на задачу — вся внутренняя логика (services/*, bitrix_client) уже async/await.
    """
    try:
        asyncio.run(_process(member_id, module_name, payload))
    except Exception as exc:  # noqa: BLE001 — осознанный catch-all для ретрая Celery
        raise self.retry(exc=exc)


async def _process(member_id: str, module_name: str, payload: dict) -> None:
    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.member_id == member_id))
        client = result.scalar_one_or_none()

    if client is None or not client.is_active:
        return  # клиент удалил приложение между постановкой задачи и её выполнением

    handler = MODULE_HANDLERS.get(module_name)
    if handler is None:
        return  # неизвестный модуль — не должно происходить, но не роняем воркер

    await handler(client, payload)
