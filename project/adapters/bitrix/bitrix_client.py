"""
Обёртка над Bitrix24 REST API.

Ключевое архитектурное решение (зафиксировано в обсуждении):
Рефреш токена НЕ держит блокировку строки в БД (никакого SELECT ... FOR UPDATE)
на время HTTP-запроса к oauth.bitrix.info. Если Bitrix начнёт отвечать медленно,
это не должно ставить в очередь остальные вебхуки этого клиента.

Вместо этого — optimistic CAS (compare-and-swap) на UPDATE:
мы обновляем токены только если refresh_token в БД всё ещё равен тому, что мы
только что использовали. Если кто-то другой (параллельный запрос) уже успел
обновить токен раньше нас — наш UPDATE просто не найдёт строку (rowcount == 0),
и мы читаем уже свежие токены из БД вместо повторного похода в Bitrix.
"""
from __future__ import annotations

import asyncio

import httpx
from sqlalchemy import select, update

from config import BITRIX_CLIENT_ID, BITRIX_CLIENT_SECRET, BITRIX_OAUTH_TOKEN_URL
from core.db import get_session
from core.models import Client

# In-memory лок на member_id — защищает от лишних параллельных рефрешей ВНУТРИ
# одного процесса (не защищает от гонки между несколькими воркерами/процессами —
# от этого защищает CAS на UPDATE выше).
_refresh_locks: dict[str, asyncio.Lock] = {}


def _lock_for(member_id: str) -> asyncio.Lock:
    return _refresh_locks.setdefault(member_id, asyncio.Lock())


class BitrixAuthError(Exception):
    """Не удалось получить валидный токен для клиента (например, клиент удалил приложение)."""


async def _refresh_tokens(member_id: str) -> Client:
    """
    Обновляет access/refresh токены клиента через CAS-update.
    Возвращает актуальный объект Client (с новыми или уже кем-то обновлёнными токенами).
    """
    async with _lock_for(member_id):
        async with get_session() as session:
            result = await session.execute(select(Client).where(Client.member_id == member_id))
            client = result.scalar_one_or_none()
            if client is None or not client.is_active:
                raise BitrixAuthError(f"Клиент {member_id} не найден или деактивирован")
            old_refresh_token = client.refresh_token

        # HTTP-запрос идёт ВНЕ транзакции/сессии — не держим соединение с БД занятым.
        async with httpx.AsyncClient(timeout=15.0) as http:
            response = await http.post(
                BITRIX_OAUTH_TOKEN_URL,
                params={
                    "grant_type": "refresh_token",
                    "client_id": BITRIX_CLIENT_ID,
                    "client_secret": BITRIX_CLIENT_SECRET,
                    "refresh_token": old_refresh_token,
                },
            )
        response.raise_for_status()
        data = response.json()
        new_access = data["access_token"]
        new_refresh = data["refresh_token"]

        async with get_session() as session:
            cas_result = await session.execute(
                update(Client)
                .where(Client.member_id == member_id, Client.refresh_token == old_refresh_token)
                .values(access_token=new_access, refresh_token=new_refresh)
            )
            await session.commit()

            if cas_result.rowcount == 0:
                # Кто-то параллельно уже обновил токен раньше нас — не ошибка,
                # просто перечитываем актуальную запись.
                result = await session.execute(select(Client).where(Client.member_id == member_id))
                return result.scalar_one()

            result = await session.execute(select(Client).where(Client.member_id == member_id))
            return result.scalar_one()


async def call_bitrix_method(client: Client, method: str, params: dict | None = None) -> dict:
    """
    Вызывает метод Bitrix24 REST API от имени клиента.
    При 401 (просроченный access_token) автоматически рефрешит токен и повторяет запрос один раз.
    """
    params = params or {}
    url = f"https://{client.domain}/rest/{method}.json"

    async def _do_request(access_token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15.0) as http:
            return await http.post(url, data={**params, "auth": access_token})

    response = await _do_request(client.access_token)

    if response.status_code == 401:
        client = await _refresh_tokens(client.member_id)
        response = await _do_request(client.access_token)

    response.raise_for_status()
    payload = response.json()
    if "error" in payload:
        raise RuntimeError(f"Ошибка Bitrix API ({method}): {payload}")
    return payload
