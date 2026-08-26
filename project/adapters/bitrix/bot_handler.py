"""
Единственная точка входа для событий чат-ботов Bitrix24.

ФОРМАТ ПОДТВЕРЖДЁН НА РЕАЛЬНОМ ПОРТАЛЕ (2026-08-26): событие ONIMBOTV2MESSAGEADD
приходит НЕ JSON-телом, как предполагалось изначально, а form-urlencoded с
bracket-нотацией в именах ключей — точно так же, как /oauth/install. Разбираем
его тем же способом: читаем плоские ключи вида "data[message][text]" напрямую
через request.form(), без сборки настоящего вложенного dict.

Подтверждённые на реальном событии ключи:
  data[bot][id]                    — какой бот получил сообщение
  data[bot][auth][domain]          — домен портала
                                      ВАЖНО: auth лежит ВНУТРИ data[bot][auth],
                                      а НЕ отдельным top-level полем "auth" —
                                      это тоже отличается от первого предположения.
  data[bot][auth][application_token]
  data[message][id]                — id сообщения
  data[message][text]              — текст сообщения
  data[message][authorId]          — id автора (camelCase-вариант подтверждён;
                                      в событии также дублируется snake_case
                                      author_id — используем camelCase)
  data[chat][id]                   — id чата
  data[chat][dialogId]             — id диалога (для imbot.v2.Chat.Message.send)
  data[chat][entityType]           — 'LINES' для Открытых линий, иначе пусто

Ключевое решение (зафиксировано в обсуждении): маршрутизация идёт по бот-id,
а НЕ циклом по всем enabled_modules клиента — у каждого модуля свой отдельный бот
(см. auth.py/_ensure_bots_registered), поэтому входящее сообщение однозначно
принадлежит ровно одному модулю.

Хендлер максимально тонкий: валидация + постановка задачи в очередь + мгновенный
200 OK. Вся тяжёлая логика — асинхронно в Celery-воркере (tasks/dispatch.py).
Это защищает от Bitrix retry при медленном ответе (дублирование сообщений)
и от блокировки event loop FastAPI при недоступности Bitrix.
"""
from __future__ import annotations

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
    form = await request.form()

    if not form:
        # Пустое тело — служебный/пинг-запрос (например, при выходе Render
        # из сна) либо обрыв соединения на стороне Bitrix. Не наша ошибка,
        # но и не событие, которое можно обработать.
        logger.warning("Пустой запрос на /bot/handler — пропускаем.")
        return {"result": True}

    event = form.get("event")
    logger.info("Получено событие на /bot/handler: %s, поля: %s", event, dict(form))

    if event != "ONIMBOTV2MESSAGEADD":
        # Другие события (ONIMBOTV2JOINCHAT и т.п.) сюда тоже могут прийти
        # в будущем — пока просто подтверждаем получение и не обрабатываем.
        return {"result": True}

    bot_id_raw = form.get("data[bot][id]")
    domain = form.get("data[bot][auth][domain]")
    application_token = form.get("data[bot][auth][application_token]")

    if not domain or not application_token or bot_id_raw is None:
        logger.warning("Неполные данные события ONIMBOTV2MESSAGEADD — пропускаем.")
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

    # Собираем плоский payload с нужными полями для задачи — дальше по конвейеру
    # (tasks/dispatch.py, services/*) работаем с этими простыми ключами, а не
    # с сырыми bracket-именами формы.
    payload = {
        "message_id": form.get("data[message][id]"),
        "message_text": form.get("data[message][text]"),
        "author_id": form.get("data[message][authorId]"),
        "chat_id": form.get("data[chat][id]"),
        "dialog_id": form.get("data[chat][dialogId]"),
        "entity_type": form.get("data[chat][entityType]"),
    }

    # Ставим задачу в очередь и сразу отвечаем Bitrix — не ждём выполнения.
    process_bot_message.delay(member_id=client.member_id, module_name=module_name, payload=payload)

    return {"result": True}
