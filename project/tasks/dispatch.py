"""
Диспетчер задач, пришедших от Bitrix-адаптера.

Это единственный файл в tasks/, который знает про Bitrix (через Client-модель
и services.MODULE_HANDLERS). Будущие не-Bitrix модули (например, крипто-кошелёк
парсер) получат свой отдельный файл здесь же — tasks/wallet_tasks.py — который
не будет ничего импортировать из adapters/bitrix/ или знать про Client.
Общая для всех — только core/queue.py (сам Celery app).
"""
from __future__ import annotations

from sqlalchemy import select

from core.async_utils import run_async
from core.db import get_session
from core.models import Client
from core.queue import celery_app
from services import MODULE_HANDLERS


@celery_app.task(name="tasks.process_bot_message", bind=True, max_retries=3, default_retry_delay=10)
def process_bot_message(self, member_id: str, module_name: str, payload: dict) -> None:
    """
    Синхронная обёртка (Celery-таск) вокруг асинхронной обработки.
    В обычном режиме Celery-воркер сам по себе синхронный (нет своего event loop),
    поэтому запуск async-корутины здесь безопасен напрямую. Но эта же функция
    вызывается и в eager-режиме (см. config.CELERY_TASK_ALWAYS_EAGER) прямо из
    async-обработчика FastAPI — там уже ЕСТЬ работающий event loop, и обычный
    asyncio.run() упал бы с RuntimeError. run_async() учитывает оба случая.
    """
    try:
        run_async(_process(member_id, module_name, payload))
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
