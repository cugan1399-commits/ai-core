"""
Бизнес-логика модуля "seller": AI-агент ведёт клиента по сделке в Telegram-диалоге.

Универсальность: один и тот же код обслуживает любое направление продаж —
разница только в данных конкретного SellerPipeline (field_schema, stages,
knowledge_chunks). Ничего специфичного под "товары" или "услуги" в коде нет.

Пайплайн на каждое сообщение клиента:
1. RAG по knowledge_chunks этого pipeline (как раньше) — контекст для ответа.
2. Один вызов Claude с tool use: модель одновременно (а) формулирует ответ
   клиенту, (б) решает, какие из ещё не заполненных полей можно заполнить
   из этого сообщения, (в) решает, нужно ли сменить стадию воронки.
   Решение через инструмент update_deal, а не парсинг текста ответа — по той
   же причине, что и ESCALATION_MARKER в предыдущей версии: структурированный
   вывод надёжнее эвристик над свободным текстом.
3. Если модель не уверена в ответе по контексту — тот же ESCALATION_MARKER,
   что и раньше, эскалация оператору через imopenlines.bot.session.operator.
4. Изменения из update_deal применяются к SellerSession и синхронизируются
   в CRM (crm.deal.update) сразу, а не отложенно.

Сделка в CRM создаётся сразу при первом сообщении клиента (см. models.py:
SellerSession docstring) — до всякого AI-вызова, минимальной карточкой.
"""
from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic
from sqlalchemy import select

from adapters.bitrix.bitrix_client import call_bitrix_method
from core.db import get_session
from core.embeddings import embed_text
from core.models import Client, KnowledgeChunk, SellerPipeline, SellerSession
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ESCALATION_MARKER, RAG_TOP_K


@lru_cache(maxsize=1)
def _get_anthropic_client() -> AsyncAnthropic:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY не задан — модуль 'seller' не может отвечать без него."
        )
    return AsyncAnthropic(api_key=ANTHROPIC_API_KEY)


class AgentReply:
    """
    Результат обработки одного сообщения — то, что видит адаптер (Telegram).
    Адаптер не знает ничего про RAG/CRM/стадии — только текст и опциональные
    кнопки/флаг редактирования.
    """

    def __init__(self, text: str, actions: list[dict] | None = None, edit_previous: bool = False):
        self.text = text
        self.actions = actions or []  # [{"label": "Подтвердить заказ", "value": "confirm_order"}]
        self.edit_previous = edit_previous


UPDATE_DEAL_TOOL = {
    "name": "update_deal",
    "description": (
        "Записать данные, которые клиент только что сообщил, и/или сменить стадию сделки, "
        "если по переписке видно, что клиент дошёл до следующего этапа."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "object",
                "description": "Ключ поля из схемы -> значение, которое сообщил клиент. Указывать ТОЛЬКО новые/изменившиеся поля.",
            },
            "new_stage_key": {
                "type": "string",
                "description": "Ключ новой стадии, если пора сдвинуть воронку. Не указывать, если стадия не меняется.",
            },
        },
    },
}


def _build_system_prompt(pipeline: SellerPipeline, session: SellerSession, context: str) -> str:
    fields_desc = "\n".join(
        f"- {f['key']} ({f['label']}){' [обязательное]' if f.get('required') else ''}"
        for f in pipeline.field_schema
    )
    stages_desc = "\n".join(f"- {s['key']}: {s['description']}" for s in pipeline.stages)
    collected = ", ".join(f"{k}={v}" for k, v in session.collected_fields.items()) or "пока ничего"

    return f"""Ты — AI-продавец, ведёшь клиента к покупке в чате Telegram.

Отвечай ТОЛЬКО на основе контекста ниже (каталог/база знаний). Не придумывай факты.
Если контекста недостаточно для уверенного ответа — верни ТОЛЬКО строку {ESCALATION_MARKER}
вместо текста, без вызова инструмента.

Поля сделки, которые нужно постепенно выяснить:
{fields_desc}
Уже собрано: {collected}

Стадии воронки, в порядке прохождения:
{stages_desc}
Текущая стадия: {session.current_stage_key}

Если из сообщения клиента можно заполнить поле или пора сменить стадию — вызови
инструмент update_deal. Если ничего нового не появилось — просто ответь текстом
без вызова инструмента.

Контекст (каталог/база знаний):
{context}

Отвечай кратко, на языке клиента, и веди диалог к завершению сделки."""


async def handle(pipeline: SellerPipeline, payload: dict) -> None:
    """
    Точка входа — вызывается из tasks/seller_tasks.py.
    payload — нормализованное сообщение от Telegram-адаптера:
    {"telegram_chat_id": int, "telegram_user_id": int, "text": str, "message_id": int}
    """
    message_text = (payload.get("text") or "").strip()
    if not message_text:
        return

    session = await _get_or_create_session(pipeline, payload)

    chunks = await _retrieve_relevant_chunks(pipeline.id, message_text)
    context = "\n\n".join(f"[{c.source_type}] {c.text}" for c in chunks) or "(база знаний пуста)"

    reply, tool_input = await _generate_reply(pipeline, session, message_text, context)

    if reply == ESCALATION_MARKER:
        await _escalate_to_operator(pipeline, session)
        return

    if tool_input:
        await _apply_deal_update(pipeline, session, tool_input)

    # Синхронизация в Open Lines (через imbot, диалог уже создан в imconnector-мосте)
    if session.bitrix_dialog_id:
        client = await _get_client(pipeline.member_id)
        bot_id = client.bot_ids["seller"]["id"]
        await call_bitrix_method(
            client,
            "imbot.v2.Chat.Message.send",
            {"botId": bot_id, "dialogId": session.bitrix_dialog_id, "fields[message]": reply},
        )

    # Отправку в сам Telegram и решение edit vs new message делает адаптер —
    # эта функция только возвращает AgentReply дальше по цепочке.
    return AgentReply(text=reply)


async def _get_or_create_session(pipeline: SellerPipeline, payload: dict) -> SellerSession:
    telegram_chat_id = payload["telegram_chat_id"]
    async with get_session() as db:
        result = await db.execute(
            select(SellerSession).where(
                SellerSession.pipeline_id == pipeline.id,
                SellerSession.telegram_chat_id == telegram_chat_id,
                SellerSession.status == "active",
            )
        )
        session = result.scalar_one_or_none()
        if session is not None:
            return session

        # Новый клиент — сразу создаём минимальную сделку в CRM (см. докстринг
        # SellerSession в models.py: осознанное решение делать это сразу, а не
        # после сбора всех полей).
        client = await _get_client(pipeline.member_id)
        deal = await call_bitrix_method(
            client,
            "crm.deal.add",
            {"fields[CATEGORY_ID]": pipeline.bitrix_category_id, "fields[TITLE]": "Заявка из Telegram"},
        )

        session = SellerSession(
            pipeline_id=pipeline.id,
            telegram_chat_id=telegram_chat_id,
            bitrix_deal_id=deal["result"],
            current_stage_key=pipeline.stages[0]["key"],
        )
        db.add(session)
        await db.commit()
        await db.refresh(session)
        return session


async def _get_client(member_id: str) -> Client:
    async with get_session() as db:
        result = await db.execute(select(Client).where(Client.member_id == member_id))
        return result.scalar_one()


async def _retrieve_relevant_chunks(pipeline_id: int, question: str) -> list[KnowledgeChunk]:
    query_embedding = embed_text(question)
    async with get_session() as db:
        result = await db.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.pipeline_id == pipeline_id)
            .order_by(KnowledgeChunk.embedding.cosine_distance(query_embedding))
            .limit(RAG_TOP_K)
        )
        return list(result.scalars().all())


async def _generate_reply(
    pipeline: SellerPipeline, session: SellerSession, message_text: str, context: str
) -> tuple[str, dict | None]:
    system_prompt = _build_system_prompt(pipeline, session, context)

    response = await _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=500,
        system=system_prompt,
        tools=[UPDATE_DEAL_TOOL],
        messages=[{"role": "user", "content": message_text}],
    )

    text_parts = [b.text for b in response.content if b.type == "text"]
    tool_calls = [b for b in response.content if b.type == "tool_use"]

    reply_text = "".join(text_parts).strip()
    tool_input = tool_calls[0].input if tool_calls else None

    return reply_text, tool_input


async def _apply_deal_update(pipeline: SellerPipeline, session: SellerSession, tool_input: dict) -> None:
    new_fields = tool_input.get("fields") or {}
    new_stage_key = tool_input.get("new_stage_key")

    update_payload: dict = {}
    if new_fields:
        session.collected_fields = {**session.collected_fields, **new_fields}
        schema_by_key = {f["key"]: f for f in pipeline.field_schema}
        for key, value in new_fields.items():
            bitrix_field = schema_by_key.get(key, {}).get("bitrix_field")
            if bitrix_field:
                update_payload[f"fields[{bitrix_field}]"] = value

    if new_stage_key and new_stage_key != session.current_stage_key:
        session.current_stage_key = new_stage_key
        stage_by_key = {s["key"]: s for s in pipeline.stages}
        bitrix_stage_id = stage_by_key.get(new_stage_key, {}).get("bitrix_stage_id")
        if bitrix_stage_id:
            update_payload["fields[STAGE_ID]"] = bitrix_stage_id

    async with get_session() as db:
        db.add(session)
        await db.commit()

    if update_payload and session.bitrix_deal_id:
        client = await _get_client(pipeline.member_id)
        update_payload["id"] = session.bitrix_deal_id
        await call_bitrix_method(client, "crm.deal.update", update_payload)


async def _escalate_to_operator(pipeline: SellerPipeline, session: SellerSession) -> None:
    client = await _get_client(pipeline.member_id)
    bot_token = client.bot_ids["seller"]["token"]
    await call_bitrix_method(
        client,
        "imopenlines.bot.session.operator",
        {"CHAT_ID": session.bitrix_chat_id, "BOT_TOKEN": bot_token},
    )