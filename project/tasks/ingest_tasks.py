"""
Наполнение knowledge_chunks для модуля 'seller'.

Два источника:
1. Каталог Bitrix CRM (crm.product.list) — синхронизируется периодической
   Celery-задачей ingest_catalog_task для каждого клиента с активным модулем 'seller'.
2. Своя База Знаний — текстовые документы, добавляемые вручную через ingest_kb_text()
   (например, из будущей админки/скрипта загрузки).

Оба источника проходят один и тот же путь: текст → эмбеддинг → upsert в knowledge_chunks
по (member_id, source_type, source_id) — см. core/models.py.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adapters.bitrix.bitrix_client import call_bitrix_method
from core.async_utils import run_async
from core.db import get_session
from core.embeddings import embed_texts
from core.models import Client, KnowledgeChunk
from core.queue import celery_app


def _product_to_text(product: dict) -> str:
    """Собирает карточку товара в один текстовый чанк для эмбеддинга/поиска."""
    name = product.get("NAME", "")
    description = product.get("DESCRIPTION") or ""
    price = product.get("PRICE")
    price_line = f"Цена: {price}" if price else ""
    return f"{name}\n{description}\n{price_line}".strip()


async def _upsert_chunks(member_id: str, source_type: str, items: list[tuple[str, str]]) -> None:
    """
    items — список (source_id, text). Эмбеддинги считаются одним батчем (дешевле по CPU),
    затем каждая строка upsert'ится по уникальному индексу (member_id, source_type, source_id).
    """
    if not items:
        return

    texts = [text for _, text in items]
    embeddings = embed_texts(texts)

    async with get_session() as session:
        for (source_id, text), embedding in zip(items, embeddings):
            stmt = pg_insert(KnowledgeChunk).values(
                member_id=member_id,
                source_type=source_type,
                source_id=source_id,
                text=text,
                embedding=embedding,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["member_id", "source_type", "source_id"],
                set_={"text": stmt.excluded.text, "embedding": stmt.excluded.embedding},
            )
            await session.execute(stmt)
        await session.commit()


async def _ingest_catalog_for_client(client: Client) -> None:
    start = 0
    items: list[tuple[str, str]] = []

    while True:
        response = await call_bitrix_method(
            client, "crm.product.list", {"start": start, "select[]": ["ID", "NAME", "DESCRIPTION", "PRICE"]}
        )
        products = response.get("result", [])
        if not products:
            break

        items.extend((str(p["ID"]), _product_to_text(p)) for p in products)

        next_start = response.get("next")
        if next_start is None:
            break
        start = next_start

    await _upsert_chunks(client.member_id, "catalog", items)


@celery_app.task(name="tasks.ingest_catalog_task")
def ingest_catalog_task() -> None:
    """
    Периодическая задача (запускать по расписанию Celery beat, например раз в сутки):
    синхронизирует каталог для ВСЕХ клиентов с активным модулем 'seller'.

    ПРИМЕЧАНИЕ (Free-тариф без Background Worker): без отдельного beat-процесса эта
    задача не запустится сама по себе ни разу. Пока в таком режиме — дёргай её вручную
    через временный админ-эндпоинт (см. README, раздел "Free-тариф без воркера").
    """
    run_async(_ingest_all_catalogs())


async def _ingest_all_catalogs() -> None:
    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.is_active.is_(True)))
        clients = list(result.scalars().all())

    for client in clients:
        if "seller" in client.enabled_modules:
            await _ingest_catalog_for_client(client)


async def ingest_kb_text(member_id: str, document_id: str, text: str) -> None:
    """
    Добавляет/обновляет один документ Базы Знаний. Вызывается вручную —
    например, из будущего скрипта загрузки файлов или простой админки.
    Один документ = один чанк; для длинных документов вызывающий код должен
    сам разбить текст на смысловые куски и вызвать это по одному на каждый.
    """
    embedding = embed_texts([text])[0]
    async with get_session() as session:
        stmt = pg_insert(KnowledgeChunk).values(
            member_id=member_id,
            source_type="kb",
            source_id=document_id,
            text=text,
            embedding=embedding,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["member_id", "source_type", "source_id"],
            set_={"text": stmt.excluded.text, "embedding": stmt.excluded.embedding},
        )
        await session.execute(stmt)
        await session.commit()
