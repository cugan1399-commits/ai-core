"""
Бизнес-логика модуля "seller": AI-агент отвечает на вопросы клиента в чате
Открытой линии по каталогу товаров/услуг и Базе Знаний.

Пайплайн (RAG):
1. Эмбеддинг входящего вопроса (core/embeddings.py — локальная модель, без API).
2. Векторный поиск top-k похожих чанков в knowledge_chunks (и 'catalog', и 'kb' —
   один и тот же поиск по обоим источникам, они не различаются на этом шаге).
3. Промпт в Claude с этими чанками как единственным источником контекста.
4. Если модель не может ответить по контексту — она возвращает структурированный
   маркер ESCALATION_MARKER (см. config.py) вместо текста. Мы не парсим текст
   ответа на признаки неуверенности — это ненадёжно; вместо этого прямо просим
   модель вернуть маркер, когда контекста недостаточно.
5. При маркере — передаём диалог живому оператору через imopenlines.bot.session.operator.
   Иначе — отправляем ответ через imbot.v2.Chat.Message.send.

ВАЖНО (требует проверки перед продакшеном): точные параметры
imopenlines.bot.session.operator (в частности, обязательность botToken vs
CLIENT_ID в теле запроса при OAuth-приложении, а не входящем вебхуке) стоит
свериться с реальным вызовом на тестовом портале — офдокументация по этому
методу на момент написания не даёт полного списка полей.
"""
from __future__ import annotations

from functools import lru_cache

from anthropic import AsyncAnthropic
from sqlalchemy import select

from adapters.bitrix.bitrix_client import call_bitrix_method
from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ESCALATION_MARKER, RAG_TOP_K
from core.db import get_session
from core.embeddings import embed_text
from core.models import Client, KnowledgeChunk


@lru_cache(maxsize=1)
def _get_anthropic_client() -> AsyncAnthropic:
    """
    Ленивая инициализация — модуль testing_service (и весь остальной сервис)
    должен уметь стартовать и работать без ANTHROPIC_API_KEY, если seller-модуль
    ещё не используется (например, на этапе тестирования на Free-тарифе перед
    тем, как оплачивать API). Падаем только тут, в момент реального вызова.
    """
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY не задан — модуль 'seller' не может отвечать без него. "
            "Задай переменную окружения перед активацией этого модуля клиенту."
        )
    return AsyncAnthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = f"""Ты — AI-консультант компании, отвечаешь клиентам в чате.
Отвечай ТОЛЬКО на основе предоставленного ниже контекста (каталог товаров/услуг
и База Знаний компании). Не придумывай факты, которых нет в контексте.

Если в контексте недостаточно информации, чтобы уверенно ответить на вопрос —
верни ТОЛЬКО строку {ESCALATION_MARKER} без какого-либо другого текста.
Не пытайся угадывать или отвечать общими фразами в этом случае — только маркер.

Отвечай кратко и по делу, на языке вопроса клиента."""


async def handle(client: Client, payload: dict) -> None:
    """
    Точка входа модуля — вызывается из tasks/dispatch.py.
    payload — это словарь `data` из события ONIMBOTV2MESSAGEADD.
    """
    message_text = (payload["message"].get("text") or "").strip()
    dialog_id = payload["chat"]["dialogId"]
    chat_id = payload["chat"]["id"]
    bot_id = client.bot_ids["seller"]["id"]

    if not message_text:
        return

    chunks = await _retrieve_relevant_chunks(client.member_id, message_text)
    answer = await _generate_answer(message_text, chunks)

    if answer.strip() == ESCALATION_MARKER:
        await _escalate_to_operator(client, chat_id)
        return

    await call_bitrix_method(
        client,
        "imbot.v2.Chat.Message.send",
        {"botId": bot_id, "dialogId": dialog_id, "fields[message]": answer},
    )


async def _retrieve_relevant_chunks(member_id: str, question: str) -> list[KnowledgeChunk]:
    query_embedding = embed_text(question)

    async with get_session() as session:
        result = await session.execute(
            select(KnowledgeChunk)
            .where(KnowledgeChunk.member_id == member_id)
            .order_by(KnowledgeChunk.embedding.cosine_distance(query_embedding))
            .limit(RAG_TOP_K)
        )
        return list(result.scalars().all())


async def _generate_answer(question: str, chunks: list[KnowledgeChunk]) -> str:
    if not chunks:
        # Пустая база знаний у клиента — не тратим вызов LLM, сразу эскалируем.
        return ESCALATION_MARKER

    context = "\n\n".join(f"[{c.source_type}] {c.text}" for c in chunks)

    response = await _get_anthropic_client().messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": f"Контекст:\n{context}\n\nВопрос клиента: {question}"}],
    )
    return "".join(block.text for block in response.content if block.type == "text")


async def _escalate_to_operator(client: Client, chat_id: int) -> None:
    """
    Передаёт диалог свободному оператору. Требует botToken того же бота,
    что зарегистрирован для модуля 'seller' (см. core/models.py: bot_ids).
    """
    bot_token = client.bot_ids["seller"]["token"]
    await call_bitrix_method(
        client,
        "imopenlines.bot.session.operator",
        {"CHAT_ID": chat_id, "BOT_TOKEN": bot_token},
    )
