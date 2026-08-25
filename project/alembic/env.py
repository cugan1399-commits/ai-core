"""
Alembic env.py — подключает Base.metadata из core.models, чтобы
`alembic revision --autogenerate` видел актуальные модели.
"""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from config import DATABASE_URL
from core.db import Base
from core import models  # noqa: F401 — импорт нужен, чтобы модели зарегистрировались в Base.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    # pgvector — расширение Postgres, требуется таблице knowledge_chunks (core/models.py).
    # Создаём его здесь, а не полагаемся на автогенерацию Alembic (она не видит
    # CREATE EXTENSION — это не часть SQLAlchemy metadata).
    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = create_async_engine(DATABASE_URL)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
