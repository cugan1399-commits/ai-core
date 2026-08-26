"""
Единственная точка входа для событий чат-ботов Bitrix24.

МИГРАЦИЯ v1 → v2: событие теперь ONIMBOTV2MESSAGEADD (не ONIMBOTMESSAGEADD),
приходит JSON-телом (не form-urlencoded), поля camelCase и вложенные:
  data.bot.id           — какой бот получил сообщение (замена BOT_ID)
  data.message.text      — текст сообщения (замена MESSAGE)
  data.message.id        — id сообщения
  data.message.authorId  — id автора сообщения (замена USER_ID)
                            ЗАФИКСИРОВАТЬ ПЕРЕД ПРОДАКШЕНОМ: точное имя поля
                            для id автора нужно свериться с реальным событием
                            в тестовом портале — в официальных примерах явно
                            не приводится, здесь — обоснованное предположение
                            по общей конвенции API v2 (camelCase + "...Id").
  data.chat.id            — id чата (используется в imopenlines.bot.session.*)
  data.chat.dialogId      — id диалога (используется в imbot.v2.Chat.Message.send)
  data.chat.entityType    — 'LINES' для Открытых линий, иначе — обычный чат/личка

Ключевое решение (зафиксировано в обсуждении): маршрутизация идёт по data.bot.id,
а НЕ циклом по всем enabled_modules клиента — у каждого модуля свой отдельный бот
(см. auth.py/_ensure_bots_registered), поэтому входящее сообщение однозначно
принадлежит ровно одному модулю.

Хендлер максимально тонкий: валидация + постановка задачи в очередь + мгновенный
200 OK. Вся тяжёлая логика — асинхронно в Celery-воркере (tasks/dispatch.py).
Это защищает от Bitrix retry при медленном ответе (дублирование сообщений)
и от блокировки event loop FastAPI при недоступности Bitrix.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from core.db import get_session
from core.models import Client
from tasks.dispatch import process_bot_message

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/bot/handler")
async def bot_handler(request: Request):
    raw_body = await request.body()

    if not raw_body:
        # Пустое тело — служебный/пинг-запрос (например, от Render при выходе
        # из сна) либо обрыв соединения на стороне Bitrix. Не наша ошибка,
        # но и не событие, которое можно обработать — просто подтверждаем
        # получение и не падаем 500-кой.
        logger.warning("Пустое тело запроса на /bot/handler — пропускаем.")
        return {"result": True}

    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError:
        # ВРЕМЕННО, для диагностики реального формата события: логируем как есть,
        # включая случай form-urlencoded вместо JSON — уберём после того как
        # сверим точный формат ONIMBOTV2MESSAGEADD на этом портале.
        logger.warning("Не удалось распарсить тело /bot/handler как JSON: %r", raw_body[:2000])
        return {"result": True}

    logger.info("Получено событие на /bot/handler: %s", body)

    event = body.get("event")
    if event != "ONIMBOTV2MESSAGEADD":
        # Другие события (ONIMBOTV2JOINCHAT и т.п.) сюда тоже могут прийти
        # в будущем — пока просто подтверждаем получение и не обрабатываем.
        return {"result": True}

    data = body.get("data", {})
    auth = body.get("auth", {})

    domain = auth.get("domain")
    application_token = auth.get("application_token")
    bot_id_raw = data.get("bot", {}).get("id")

    if not domain or not application_token or bot_id_raw is None:
        return {"result": True}

    async with get_session() as session:
        result = await session.execute(
            select(Client).where(Client.domain == domain, Client.is_active.is_(True))
        )
        client = result.scalar_one_or_none()

    if client is None:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    if application_token != client.application_token:
        raise HTTPException(status_code=403, detail="Неверный application_token")

    incoming_bot_id = int(bot_id_raw)
    module_name = next(
        (name for name, bot in client.bot_ids.items() if bot["id"] == incoming_bot_id),
        None,
    )
    if module_name is None:
        # Событие от неизвестного/чужого бота — игнорируем, это не ошибка.
        return {"result": True}

    # Ставим задачу в очередь и сразу отвечаем Bitrix — не ждём выполнения.
    process_bot_message.delay(member_id=client.member_id, module_name=module_name, payload=data)

    return {"result": True}
