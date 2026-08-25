"""
OAuth-хендшейк с Bitrix24.

Ключевые решения:
- Одно приложение (BITRIX_CLIENT_ID/SECRET) на всех клиентов — маркетплейс-тип,
  а не локальное приложение под каждый портал.
- /oauth/install идемпотентен: повторная установка не плодит дубли клиента и не
  плодит дубли ботов — регистрация бота проверяется по каждому МОДУЛЮ отдельно
  (bot_ids[module]), а не по факту "хоть один бот уже есть".
- /oauth/uninstall не удаляет клиента физически (история сессий тестирования
  может быть нужна), а помечает is_active=False.
- Регистрация бота — через imbot.v2.Bot.register (актуальный API; imbot.register
  из первой версии этого файла помечен Bitrix как устаревший). Регистрируем в
  гибридном режиме (type='bot', isSupportOpenline=true), чтобы бот одинаково
  работал и в Открытых линиях, и в обычных диалогах.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Request
from sqlalchemy import select

from adapters.bitrix.bitrix_client import call_bitrix_method
from config import ALLOWED_MODULES
from core.db import get_session
from core.models import Client

logger = logging.getLogger(__name__)
router = APIRouter()

# На установке активируем этот набор модулей по умолчанию для НОВОГО клиента.
# Пусто — активация модулей отдельный сознательный шаг (например, через админку),
# а не побочный эффект установки приложения.
DEFAULT_MODULES_ON_INSTALL: list[str] = []


@router.post("/oauth/install")
async def oauth_install(request: Request):
    """
    Реальный формат подтверждён логами тестового портала: Bitrix шлёт событие
    ONAPPINSTALL, все нужные данные — во вложенных (по имени ключа) полях
    auth[...], а НЕ в плоских DOMAIN/MEMBER_ID/AUTH_ID/REFRESH_ID/APP_SID,
    как ошибочно предполагалось в первой версии этого файла (то было взято
    из исходного ТЗ без сверки с реальным вызовом).

    form-urlencoded не поддерживает вложенность нативно — Bitrix кодирует её
    прямо в имя ключа (буквально строка "auth[domain]"), поэтому и читаем
    так же, строкой, а не как настоящий вложенный dict.
    """
    form = await request.form()
    logger.info("Получен запрос на /oauth/install, событие: %s, поля: %s", form.get("event"), dict(form))

    domain = form.get("auth[domain]")
    member_id = form.get("auth[member_id]")
    access_token = form.get("auth[access_token]")
    refresh_token = form.get("auth[refresh_token]")
    application_token = form.get("auth[application_token]", "")

    if not all([domain, member_id, access_token, refresh_token]):
        # Неполный запрос (служебный пинг Bitrix или другое событие) — не падаем,
        # просто подтверждаем получение. Смотри Render-логи по строке выше.
        logger.warning(
            "Неполные данные установки (нет одного из auth[domain]/auth[member_id]/"
            "auth[access_token]/auth[refresh_token]) — пропускаем создание клиента."
        )
        return {"result": True}

    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.member_id == member_id))
        client = result.scalar_one_or_none()

        if client is None:
            client = Client(
                member_id=member_id,
                domain=domain,
                access_token=access_token,
                refresh_token=refresh_token,
                application_token=application_token,
                enabled_modules=list(DEFAULT_MODULES_ON_INSTALL),
                bot_ids={},
                is_active=True,
            )
            session.add(client)
        else:
            # Переустановка: обновляем токены и домен, реактивируем клиента.
            client.domain = domain
            client.access_token = access_token
            client.refresh_token = refresh_token
            client.application_token = application_token
            client.is_active = True

        await session.commit()
        await session.refresh(client)

        await _ensure_bots_registered(session, client)
        await session.commit()

    return {"result": True}


async def _ensure_bots_registered(session, client: Client) -> None:
    """
    Регистрирует бота (imbot.v2.Bot.register) для каждого активного модуля клиента,
    у которого ещё нет сохранённого bot_id. Идемпотентно на уровне (client, module) —
    повторный вызов ничего не задублирует.
    """
    bot_ids = dict(client.bot_ids)
    changed = False

    for module_name in client.enabled_modules:
        if module_name in bot_ids:
            continue  # бот для этого модуля уже зарегистрирован ранее

        bot_token = secrets.token_hex(16)  # до 40 символов, требование Bitrix — укладываемся

        response = await call_bitrix_method(
            client,
            "imbot.v2.Bot.register",
            {
                "fields[code]": f"{module_name}_bot_{client.member_id}",
                "fields[botToken]": bot_token,
                "fields[type]": "bot",  # гибридный режим: групповые чаты + личка + Open Lines
                "fields[isSupportOpenline]": "true",
                "fields[eventMode]": "webhook",
                "fields[webhookUrl]": f"{_public_base_url()}/bot/handler",
                "fields[properties][name]": f"AI {module_name}",
            },
        )
        bot_ids[module_name] = {"id": response["result"]["bot"]["id"], "token": bot_token}
        changed = True

    if changed:
        client.bot_ids = bot_ids
        session.add(client)


def _public_base_url() -> str:
    import os

    return os.environ["PUBLIC_BASE_URL"]  # например: https://your-vps.example.com


@router.post("/oauth/uninstall")
async def oauth_uninstall(request: Request):
    form = await request.form()
    logger.info("Получен запрос на /oauth/uninstall, событие: %s, поля: %s", form.get("event"), dict(form))

    member_id = form.get("auth[member_id]") or form.get("MEMBER_ID") or form.get("member_id")

    if not member_id:
        logger.warning("Не удалось найти member_id в запросе на /oauth/uninstall — смотри поля выше.")
        return {"result": True}

    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.member_id == member_id))
        client = result.scalar_one_or_none()
        if client is not None:
            client.is_active = False
            session.add(client)
            await session.commit()

    return {"result": True}


async def activate_module(member_id: str, module_name: str) -> None:
    """
    Включает модуль клиенту и регистрирует под него бота, если ещё не был
    зарегистрирован. Вызывается вручную (админкой/скриптом) — активация
    модуля никогда не происходит автоматически.
    """
    if module_name not in ALLOWED_MODULES:
        raise ValueError(f"Неизвестный модуль: {module_name}")

    async with get_session() as session:
        result = await session.execute(select(Client).where(Client.member_id == member_id))
        client = result.scalar_one()

        if module_name not in client.enabled_modules:
            client.enabled_modules = [*client.enabled_modules, module_name]

        await _ensure_bots_registered(session, client)
        await session.commit()
