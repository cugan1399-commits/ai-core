import asyncio
from sqlalchemy import select
from adapters.bitrix.bitrix_client import call_bitrix_method
from core.db import get_session
from core.models import Client


async def main():
    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.member_id == "81969d52b9813de2b5c11a01f019a2f6"))
        client = result.scalar_one()

    response = await call_bitrix_method(client, "event.get")
    print(response)


asyncio.run(main())