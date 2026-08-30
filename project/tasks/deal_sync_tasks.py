"""
Синхронизация SellerSession при изменении стадии сделки в Bitrix24.

Событие ONCRMDEALUPDATE не сообщает новую стадию напрямую — только "что-то
изменилось", поэтому реальное состояние сделки всегда дозапрашивается через
crm.deal.get. Используется поле CLOSED ('Y'/'N') — оно true и для успеха, и
для провала, что ровно соответствует требованию "новая сделка только после
завершения предыдущей, успешно или нет".
"""
from __future__ import annotations

from sqlalchemy import select, update

from adapters.bitrix.bitrix_client import call_bitrix_method
from core.async_utils import run_async
from core.db import get_session
from core.models import Client, SellerSession
from core.queue import celery_app


@celery_app.task(name="tasks.sync_deal_stage", bind=True, max_retries=3, default_retry_delay=10)
def sync_deal_stage(self, member_id: str, deal_id: int) -> None:
    try:
        run_async(_sync(member_id, deal_id))
    except Exception as exc:  # noqa: BLE001
        raise self.retry(exc=exc)


async def _sync(member_id: str, deal_id: int) -> None:
    async with get_session() as db:
        result = await db.execute(select(Client).where(Client.member_id == member_id))
        client = result.scalar_one_or_none()

    if client is None or not client.is_active:
        return

    deal = await call_bitrix_method(client, "crm.deal.get", {"id": deal_id})
    if deal["result"]["CLOSED"] != "Y":
        return  # сделка ещё в работе

    async with get_session() as db:
        await db.execute(
            update(SellerSession)
            .where(SellerSession.bitrix_deal_id == deal_id, SellerSession.status == "active")
            .values(status="completed")
        )
        await db.commit()