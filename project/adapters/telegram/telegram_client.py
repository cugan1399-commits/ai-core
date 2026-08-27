"""
Обёртка над Telegram Bot API — по аналогии с adapters/bitrix/bitrix_client.py,
только здесь не нужен OAuth/рефреш токенов: у каждого SellerPipeline свой
статичный bot token, который просто подставляется в URL метода.

Используется из tasks/seller_tasks.py — после того, как services/seller_service.py
вернул готовый AgentReply, этот модуль реально доставляет его клиенту.
"""
from __future__ import annotations

import httpx


def _build_inline_keyboard(actions: list[dict]) -> dict | None:
    if not actions:
        return None
    return {
        "inline_keyboard": [
            [{"text": action["label"], "callback_data": action["value"]}] for action in actions
        ]
    }


async def send_message(
    bot_token: str, chat_id: int, text: str, actions: list[dict] | None = None
) -> dict:
    """Отправляет новое сообщение. Возвращает ответ Telegram (содержит message_id)."""
    payload: dict = {"chat_id": chat_id, "text": text}
    keyboard = _build_inline_keyboard(actions or [])
    if keyboard:
        payload["reply_markup"] = keyboard

    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload
        )
    response.raise_for_status()
    return response.json()


async def edit_message_text(
    bot_token: str, chat_id: int, message_id: int, text: str, actions: list[dict] | None = None
) -> dict:
    """
    Редактирует уже отправленное сообщение бота вместо отправки нового —
    используется, когда AgentReply.edit_previous=True (см. seller_service.py).
    """
    payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text}
    keyboard = _build_inline_keyboard(actions or [])
    if keyboard:
        payload["reply_markup"] = keyboard

    async with httpx.AsyncClient(timeout=15.0) as http:
        response = await http.post(
            f"https://api.telegram.org/bot{bot_token}/editMessageText", json=payload
        )
    response.raise_for_status()
    return response.json()


async def answer_callback_query(bot_token: str, callback_query_id: str, text: str | None = None) -> None:
    """
    Обязательно вызывать после обработки нажатия инлайн-кнопки — иначе кнопка
    в интерфейсе Telegram будет "крутиться" (клиент видит зависшую загрузку)
    до тайм-аута.
    """
    payload: dict = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text

    async with httpx.AsyncClient(timeout=15.0) as http:
        await http.post(
            f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery", json=payload
        )
