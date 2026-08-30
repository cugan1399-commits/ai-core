"""
OAuth-хендшейк с Bitrix24.

Ключевые решения:
- Одно приложение (BITRIX_CLIENT_ID/SECRET) на всех клиентов — маркетплейс-тип,
  а не локальное приложение под каждый портал.
- Установка идемпотентна: повторная установка не плодит дубли клиента и не
  плодит дубли ботов — регистрация бота проверяется по каждому МОДУЛЮ отдельно
  (bot_ids[module]), а не по факту "хоть один бот уже есть".
- /oauth/uninstall не удаляет клиента физически (история сессий тестирования
  может быть нужна), а помечает is_active=False.
- Регистрация бота — через imbot.v2.Bot.register (актуальный API; imbot.register
  из первой версии этого файла помечен Bitrix как устаревший). Регистрируем в
  гибридном режиме (type='bot', isSupportOpenline=true), чтобы бот одинаково
  работал и в Открытых линиях, и в обычных диалогах.
- Подписка на ONCRMDEALUPDATE (см. _ensure_deal_update_event_bound) НЕ глушит
  ошибки event.bind молча — если Bitrix откажет (например, не хватает scope),
  это должно быть видно в логах, а не тихо теряться под except RuntimeError.

ВАЖНО (найдено 2026-08-30): на реальном портале Bitrix НЕ вызывает
"Путь для первоначальной установки" (/oauth/install) при обычном открытии
локального приложения (клик на его иконку) — подтверждено логами Render
(ни одного запроса на /oauth/install за несколько попыток переустановки),
в то время как "Путь вашего обработчика" (/oauth/redirect) реально
вызывается (и GET, и POST) при каждом открытии. То есть в этом сценарии
именно /oauth/redirect — фактическая точка входа установки, а /oauth/install
(если он вообще когда-то вызывается Bitrix — например, при установке из
маркетплейса, а не по прямой ссылке) — редкий/альтернативный путь. Оба
хендлера теперь используют общую _upsert_client_and_bootstrap(), но читают
токены в РАЗНЫХ форматах:
- /oauth/install ожидает вложенные ключи auth[domain], auth[access_token]
  и т.д. (этот формат подтверждён раньше для событий ONAPPINSTALL);
- /oauth/redirect ожидает ПЛОСКИЕ ключи DOMAIN, AUTH_ID, REFRESH_ID,
  member_id и т.д. — формат реально наблюдался в запросах на этот путь.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
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


async def _upsert_client_and_bootstrap(
    *, domain: str, member_id: str, access_token: str, refresh_token: str, application_token: str
) -> Client:
    """
    Общая логика для /oauth/install и /oauth/redirect: создать клиента (или
    обновить токены существующему), зарегистрировать ботов и подписаться на
    ONCRMDEALUPDATE. Вынесена в отдельную функцию, чтобы оба хендлера не
    расходились в поведении — разница между ними только в том, ОТКУДА они
    достают эти пять значений из тела запроса (см. докстринг модуля).
    """
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
        await _ensure_deal_update_event_bound(client)
        await session.commit()
        await session.refresh(client)

    return client


@router.post("/oauth/install")
async def oauth_install(request: Request):
    """
    Формат события ONAPPINSTALL (если/когда Bitrix его всё же присылает):
    вложенные по имени ключа поля auth[domain], auth[member_id] и т.д.
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

    await _upsert_client_and_bootstrap(
        domain=domain,
        member_id=member_id,
        access_token=access_token,
        refresh_token=refresh_token,
        application_token=application_token,
    )

    return {"result": True}


@router.api_route("/oauth/redirect", methods=["GET", "POST"])
async def oauth_redirect(request: Request):
    """
    Фактическая точка входа установки/открытия приложения — см. докстринг
    модуля. Bitrix шлёт сюда ПЛОСКИЕ поля (без auth[...] обёртки):
      DOMAIN       — домен портала
      member_id    — идентификатор портала (тот же member_id, что и в
                     событиях ONAPPINSTALL/imbot)
      AUTH_ID      — access_token
      REFRESH_ID   — refresh_token
      APP_SID      — application_token (используется в проверке application_token
                     у событий бота — подтверждено раньше для ONIMBOTV2MESSAGEADD)
    GET-запрос (первый заход без токенов, просто открытие ссылки на
    приложение) не содержит этих полей вообще — тогда просто отдаём страницу-
    заглушку, ничего не создавая в БД, чтобы не плодить "пустых" клиентов.
    """
    if request.method == "GET":
        params = dict(request.query_params)
    else:
        params = dict(await request.form())

    logger.info("Получен запрос на /oauth/redirect (%s), поля: %s", request.method, params)

    domain = params.get("DOMAIN")
    member_id = params.get("member_id")
    access_token = params.get("AUTH_ID")
    refresh_token = params.get("REFRESH_ID")
    application_token = params.get("APP_SID", "")

    if not all([domain, member_id, access_token, refresh_token]):
        logger.warning(
            "На /oauth/redirect нет полного набора токенов (DOMAIN/member_id/AUTH_ID/"
            "REFRESH_ID) — вероятно, просто открытие приложения без переустановки. "
            "БД не трогаем."
        )
        return HTMLResponse("<html><body>Приложение работает.</body></html>")

    await _upsert_client_and_bootstrap(
        domain=domain,
        member_id=member_id,
        access_token=access_token,
        refresh_token=refresh_token,
        application_token=application_token,
    )

    return HTMLResponse("<html><body>Приложение установлено.</body></html>")


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


async def _ensure_deal_update_event_bound(client: Client) -> None:
    """
    Подписывается на ONCRMDEALUPDATE, чтобы изменение стадии сделки в Bitrix
    (менеджер закрыл/провалил сделку вручную) закрывало соответствующую
    SellerSession в нашей БД — см. tasks/deal_sync_tasks.py.

    ВАЖНО: раньше здесь стоял `except RuntimeError: pass` с расчётом на
    "уже подписано ранее — не ошибка". На деле call_bitrix_method бросает
    RuntimeError при ЛЮБОЙ ошибке Bitrix API (см. bitrix_client.py), не
    только при повторной подписке — то есть настоящие проблемы (например,
    не хватает scope у приложения) тоже тихо проглатывались и не были видны
    в логах. Пока не подтверждено, что Bitrix ЛОЯЛЬНО обрабатывает повторный
    event.bind (обычно да — просто не плодит дубль подписки), исключение
    сознательно НЕ глушится: если что-то пойдёт не так, это будет видно.
    """
    await call_bitrix_method(
        client,
        "event.bind",
        {"event": "ONCRMDEALUPDATE", "handler": f"{_public_base_url()}/bitrix/events/deal-update"},
    )


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
