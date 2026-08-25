"""
Общая очередь задач (Celery + Redis).

ВАЖНО: этот файл не должен ничего знать про Bitrix, Telegram или конкретные фичи.
Он просто предоставляет `celery_app`, которым пользуются:
  - tasks/dispatch.py — диспетчер задач от Bitrix-адаптера
  - будущие tasks/wallet_tasks.py и другие — для личных не-Bitrix модулей

Так очередь и воркер становятся переиспользуемым инфраструктурным слоем,
а не частью Bitrix-ядра.
"""
from celery import Celery

from config import CELERY_BROKER_URL, CELERY_RESULT_BACKEND

celery_app = Celery(
    "core",
    broker=CELERY_BROKER_URL,
    backend=CELERY_RESULT_BACKEND,
    include=[
        "tasks.dispatch",
        "tasks.ingest_tasks",
        # "tasks.wallet_tasks",  # будущий не-Bitrix модуль — подключается сюда же
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_acks_late=True,          # задача считается выполненной только после успешного завершения
    worker_prefetch_multiplier=1,  # не хватать больше задач, чем воркер реально обрабатывает
    task_default_retry_delay=10,
    task_time_limit=60,
)
