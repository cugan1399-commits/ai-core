"""
Утилита для запуска async-корутины из синхронного кода Celery-задач.

Зачем это нужно: в обычном режиме Celery-воркер — отдельный процесс без
собственного event loop, там asyncio.run(...) работает без проблем.

Но в режиме CELERY_TASK_ALWAYS_EAGER=True (см. config.py — временный режим
для Free-тарифа без отдельного Background Worker) задача выполняется
СИНХРОННО прямо внутри вызова .delay(), а .delay() дёргается из
async-обработчика FastAPI (adapters/bitrix/bot_handler.py), который уже
крутится внутри event loop uvicorn. Вызов asyncio.run() изнутри уже
работающего event loop — это гарантированный RuntimeError.

run_async() определяет, есть ли уже работающий loop в текущем потоке:
- если нет — обычный asyncio.run() (штатный путь для настоящего Celery-воркера);
- если есть — выполняет корутину в отдельном потоке с собственным loop и
  блокирует текущий поток до завершения. Блокировка тут неизбежна и ожидаема:
  весь смысл eager-режима — синхронная обработка без реальной очереди.

ВАЖНО (найдено при отладке 2026-08-26): asyncpg-соединения (core/db.py)
привязаны к тому loop'у, в котором были открыты. Поэтому перед тем, как
поток с "одноразовым" loop завершится, мы явно закрываем engine, созданный
этим потоком (dispose_current_thread_engine) — ВНУТРИ того же asyncio.run(),
т.е. в том же loop'е. Если этого не сделать, соединения пытаются закрыться
позже сборщиком мусора, когда их loop уже мёртв — и падают с той же ошибкой
"attached to a different loop", просто в виде фонового необработанного
исключения вместо явной ошибки запроса.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from typing import Any, Coroutine, TypeVar

from core.db import dispose_current_thread_engine

T = TypeVar("T")


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Нет работающего loop в этом потоке — обычный путь.
        return asyncio.run(coro)

    async def _run_and_cleanup() -> T:
        try:
            return await coro
        finally:
            # Закрываем engine ЭТОГО потока, пока его loop ещё жив —
            # см. пояснение в докстринге модуля.
            await dispose_current_thread_engine()

    # Уже внутри работающего loop (eager-режим из FastAPI) — уходим в отдельный
    # поток с собственным loop, чтобы не ловить конфликт вложенных event loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, _run_and_cleanup()).result()
