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

import secrets

from fastapi import APIRouter, Request
from sqlalchemy import select

from adapters.bitrix.bitrix_client import call_bitrix_method
from config import ALLOWED_MODULES
from core.db import get_session
from core.models import Client

router = APIRouter()

# На установке активируем этот набор модулей по умолчанию для НОВОГО клиента.
# Пусто — активация модулей отдельный сознательный шаг (например, через админку),
# а не побочный эффект установки приложения.
DEFAULT_MODULES_ON_INSTALL: list[str] = []


@router.post("/oauth/install")
async def oauth_install(request: Request):
    form = await request.form()
    domain = form["DOMAIN"]
    member_id = form["MEMBER_ID"]
    access_token = form["AUTH_ID"]
    refresh_token = form["REFRESH_ID"]
    application_token = form.get("APP_SID", "")

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
    member_id = form.get("MEMBER_ID") or form.get("auth[member_id]")

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
