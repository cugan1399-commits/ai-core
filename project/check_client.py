import asyncio
from sqlalchemy import select
from core.db import get_session
from core.models import Client


async def main():
    async with get_session() as session:
        result = await session.execute(select(Client))
        for c in result.scalars().all():
            print(c.member_id, "-", c.domain)


asyncio.run(main())
