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

    Конфигурация зафиксирована для этого клиента/портала (2026-08-26):
      entityTypeId = 1038 (смарт-процесс "Аттестация менеджеров")
      ufCrm7_1787776021255 — поле "Результат теста" (число)
      ufCrm7_1787776088286 — поле "Статус аттестации" (список, enumeration).
        ID вариантов подтверждены через crm.item.fields:
          45 — "Пройдена"
          47 — "Не пройдена"
          49 — "Требуется пересдача" (пока не используется — нет критерия,
               когда назначать именно этот статус, а не "Не пройдена")

    ВАЖНО про регистр имени поля: crm.item.fields возвращает поле под ДВУМЯ
    разными именами — как ключ словаря (например, "ufCrm7_1787776021255") и
    как "upperName" (например, "UF_CRM_7_1787776021255", в стиле старых
    методов вроде crm.lead.add). Для НОВОГО универсального API (crm.item.add /
    crm.item.update — то, что используется здесь) правильный код поля — именно
    ключ словаря ("ufCrm7_..."), а не upperName. Изначально по ошибке был
    использован upperName — из-за этого запись создавалась, но оба кастомных
    поля оставались пустыми (crm.item.add молча игнорирует неизвестный ключ
    вместо явной ошибки).

    Критерий "пройдена" — все ответы верны (score == total). Если нужен другой
    порог (например, 70% и выше) или использование статуса "Требуется
    пересдача" — поменять только STATUS_PASSED/STATUS_FAILED ниже.

    Это создаёт НОВУЮ запись смарт-процесса на каждое прохождение теста
    (crm.item.add), а не ищет существующую — если нужно обновлять одну и ту же
    запись на пользователя, потребуется отдельно хранить её id (например, в
    TestSession) и вызывать crm.item.update вместо crm.item.add.
    """
    STATUS_PASSED = 45
    STATUS_FAILED = 47

    status_id = STATUS_PASSED if score == total else STATUS_FAILED

    await call_bitrix_method(
        client,
        "crm.item.add",
        {
            "entityTypeId": 1038,
            "fields[ufCrm7_1787776021255]": score,
            "fields[ufCrm7_1787776088286]": status_id,
        },
    )
