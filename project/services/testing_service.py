"""
Бизнес-логика модуля "testing": пошаговое тестирование менеджеров по Базе Знаний.

Этот файл НИЧЕГО не знает про другие модули (seller и т.д.) и ничего не знает
про Telegram/веб-каналы — только про Bitrix Open Lines, потому что сейчас
модуль работает только через этот канал.

МИГРАЦИЯ v1 → v2: отправка сообщений теперь через imbot.v2.Chat.Message.send
(botId + dialogId), а не imbot.message.add (BOT_ID + DIALOG_ID). Входящий
payload — это уже "data" из события ONIMBOTV2MESSAGEADD (см. bot_handler.py),
поля camelCase.
"""
from __future__ import annotations

from sqlalchemy import select

from adapters.bitrix.bitrix_client import call_bitrix_method
from core.db import get_session
from core.models import Client, TestSession


async def generate_questions(domain: str) -> list[dict]:
    """
    Заглушка. В будущем — реальная генерация вопросов через LLM на основе
    Базы Знаний конкретного портала. Интерфейс уже финальный: принимает domain,
    отдаёт список {"question": str, "answer": str} — вызывающий код (handle())
    менять не придётся, когда заглушку заменят на реальную генерацию.
    """
    return [
        {"question": "Сколько дней действует гарантия на товар?", "answer": "14"},
        {"question": "Какой отдел отвечает за возвраты?", "answer": "поддержка"},
    ]


async def handle(client: Client, payload: dict) -> None:
    """
    Точка входа модуля — вызывается из tasks/dispatch.py.
    payload — это словарь `data` из события ONIMBOTV2MESSAGEADD.
    """
    user_id = int(payload["message"]["authorId"])
    message_text = (payload["message"].get("text") or "").strip()
    dialog_id = payload["chat"]["dialogId"]
    bot_id = client.bot_ids["testing"]["id"]

    async with get_session() as session:
        result = await session.execute(
            select(TestSession).where(
                TestSession.domain == client.domain,
                TestSession.user_id == user_id,
                TestSession.status == "active",
            )
        )
        test_session = result.scalar_one_or_none()

        if test_session is None:
            questions = await generate_questions(client.domain)
            test_session = TestSession(
                domain=client.domain,
                user_id=user_id,
                current_step=0,
                current_score=0,
                status="active",
                questions_json=questions,
            )
            session.add(test_session)
            await session.commit()
            await session.refresh(test_session)

            await _send_message(client, bot_id, dialog_id, questions[0]["question"])
            return

        questions = test_session.questions_json
        current_question = questions[test_session.current_step]

        is_correct = message_text.lower() == str(current_question["answer"]).lower()
        new_score = test_session.current_score + (1 if is_correct else 0)
        new_step = test_session.current_step + 1

        if new_step >= len(questions):
            test_session.status = "completed"
            test_session.current_score = new_score
            test_session.current_step = new_step
            await session.commit()

            await _send_message(
                client, bot_id, dialog_id, f"Тест завершён! Ваш результат: {new_score}/{len(questions)}"
            )
            await _report_result_to_crm(client, user_id, new_score, len(questions))
            return

        test_session.current_score = new_score
        test_session.current_step = new_step
        await session.commit()

        await _send_message(client, bot_id, dialog_id, questions[new_step]["question"])


async def _send_message(client: Client, bot_id: int, dialog_id: str, text: str) -> None:
    await call_bitrix_method(
        client,
        "imbot.v2.Chat.Message.send",
        {"botId": bot_id, "dialogId": dialog_id, "fields[message]": text},
    )


async def _report_result_to_crm(client: Client, user_id: int, score: int, total: int) -> None:
    """
    Обновляет смарт-процесс аттестации в CRM клиента.
    ВАЖНО: entityTypeId и ID полей смарт-процесса — конфигурация конкретного
    портала, здесь намеренно не захардкожены. См. README про необходимость
    добавить per-client маппинг полей перед продакшен-запуском у реального клиента.
    """
    # Пример вызова, требует конфигурации per-client полей до реального использования:
    # await call_bitrix_method(client, "crm.item.update", {
    #     "entityTypeId": <entity_type_id_клиента>,
    #     "id": <id_записи_аттестации>,
    #     "fields": {"<поле_счёта>": score, "<поле_статуса>": "completed"},
    # })
    pass
