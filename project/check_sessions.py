import asyncio
from sqlalchemy import select
from core.db import get_session
from core.models import SellerPipeline, SellerSession


async def main():
    async with get_session() as session:
        pipelines = (await session.execute(select(SellerPipeline))).scalars().all()
        print("=== SellerPipeline ===")
        for p in pipelines:
            print(f"id={p.id} name={p.name} category_id={p.bitrix_category_id} active={p.is_active}")

        sessions = (await session.execute(select(SellerSession))).scalars().all()
        print("\n=== SellerSession ===")
        for s in sessions:
            print(
                f"id={s.id} pipeline_id={s.pipeline_id} chat_id={s.telegram_chat_id} "
                f"deal_id={s.bitrix_deal_id} stage={s.current_stage_key} status={s.status}"
            )


asyncio.run(main())