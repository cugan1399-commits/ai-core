"""
Наполнение knowledge_chunks для модуля 'seller'.

Скоуп синхронизации — SellerPipeline, а не Client напрямую: один клиент
(Bitrix-портал) может иметь несколько pipeline (сфер продаж), и у каждой сферы
свой набор каталогов (pipeline.bitrix_catalog_ids) — каталог одной сферы не
должен попадать в базу знаний другой сферы того же клиента.

Два источника, как и раньше:
1. Каталог(и) Bitrix CRM (crm.product.list, отфильтрованный по каждому catalog_id
   из pipeline.bitrix_catalog_ids) — периодическая синхронизация.
2. Своя База Знаний — вручную через ingest_kb_text().

Upsert идёт по (pipeline_id, source_type, source_id) — см. core/models.py.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from adapters.bitrix.bitrix_client import call_bitrix_method
from core.async_utils import run_async
from core.db import get_session
from core.embeddings import embed_texts
from core.models import Client, KnowledgeChunk, SellerPipeline
from core.queue import celery_app


def _product_to_text(product: dict) -> str:
    name = product.get("NAME", "")
    description = product.get("DESCRIPTION") or ""
    price = product.get("PRICE")
    price_line = f"Цена: {price}" if price else ""
    return f"{name}\n{description}\n{price_line}".strip()


async def _upsert_chunks(pipeline_id: int, source_type: str, items: list[tuple[str, str]]) -> None:
    if not items:
        return

    texts = [text for _, text in items]
    embeddings = embed_texts(texts)

    async with get_session() as session:
        for (source_id, text), embedding in zip(items, embeddings):
            stmt = pg_insert(KnowledgeChunk).values(
                pipeline_id=pipeline_id,
                source_type=source_type,
                source_id=source_id,
                text=text,
                embedding=embedding,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["pipeline_id", "source_type", "source_id"],
                set_={"text": stmt.excluded.text, "embedding": stmt.excluded.embedding},
            )
            await session.execute(stmt)
        await session.commit()


async def _ingest_catalog_for_pipeline(client: Client, pipeline: SellerPipeline) -> None:
    """
    Тянет товары по КАЖДОМУ catalog_id, привязанному к этому pipeline, и
    складывает всё в его knowledge_chunks одним общим пулом.

    ВАЖНО (сверить на реальном портале): здесь предполагается, что фильтр
    имеет ключ "CATALOG_ID" — на разных версиях/тарифах Bitrix это поле каталога
    товара может называться иначе (например, IBLOCK_ID в старых установках
    коммерческого каталога). Проверить реальный ответ crm.product.list с этим
    фильтром на тестовом портале перед продакшеном.
    """
    items: list[tuple[str, str]] = []

    for catalog_id in pipeline.bitrix_catalog_ids:
        start = 0
        while True:
            response = await call_bitrix_method(
                client,
                "crm.product.list",
                {
                    "filter[CATALOG_ID]": catalog_id,
                    "start": start,
                    "select[]": ["ID", "NAME", "DESCRIPTION", "PRICE"],
                },
            )
            products = response.get("result", [])
            if not products:
                break

            items.extend((str(p["ID"]), _product_to_text(p)) for p in products)

            next_start = response.get("next")
            if next_start is None:
                break
            start = next_start

    await _upsert_chunks(pipeline.id, "catalog", items)


@celery_app.task(name="tasks.ingest_catalog_task")
def ingest_catalog_task() -> None:
    """
    Периодическая задача: синхронизирует каталоги для ВСЕХ активных pipeline
    всех клиентов. Запускать по расписанию Celery beat (см. примечание в
    предыдущей версии про Free-тариф без Background Worker — актуально так же).
    """
    run_async(_ingest_all_catalogs())


async def _ingest_all_catalogs() -> None:
    async with get_session() as session:
        result = await session.execute(select(SellerPipeline).where(SellerPipeline.is_active.is_(True)))
        pipelines = list(result.scalars().all())

    # Клиентов достаём отдельно и по одному — не тянем все Client в память разом,
    # т.к. pipeline'ов может быть на порядок больше, чем клиентов.
    for pipeline in pipelines:
        async with get_session() as session:
            client_result = await session.execute(
                select(Client).where(Client.member_id == pipeline.member_id, Client.is_active.is_(True))
            )
            client = client_result.scalar_one_or_none()

        if client is not None:
            await _ingest_catalog_for_pipeline(client, pipeline)


async def ingest_kb_text(pipeline_id: int, document_id: str, text: str) -> None:
    """
    Добавляет/обновляет один документ Базы Знаний конкретного pipeline.
    Один документ = один чанк; длинные документы вызывающий код должен сам
    разбить на смысловые куски и вызвать это по одному на каждый.
    """
    embedding = embed_texts([text])[0]
    async with get_session() as session:
        stmt = pg_insert(KnowledgeChunk).values(
            pipeline_id=pipeline_id,
            source_type="kb",
            source_id=document_id,
            text=text,
            embedding=embedding,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["pipeline_id", "source_type", "source_id"],
            set_={"text": stmt.excluded.text, "embedding": stmt.excluded.embedding},
        )
        await session.execute(stmt)
        await session.commit()