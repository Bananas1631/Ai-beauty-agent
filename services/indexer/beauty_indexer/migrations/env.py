"""Alembic индексатора: только схема knowledge, своя таблица версий knowledge.alembic_version.

Миграции основного сервиса и индексатора независимы: у каждого сервиса свой релизный цикл.
Запуск: alembic -c services/indexer/alembic.ini upgrade head (make migrate делает оба).
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection

from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine
from beauty_indexer.config import get_settings
from beauty_indexer.db import SCHEMA, Base, include_name

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def postgres_settings() -> PostgresSettings:
    settings = config.attributes.get("postgres")
    return settings if isinstance(settings, PostgresSettings) else get_settings().postgres


def do_run_migrations(connection: Connection) -> None:
    # Таблица версий живёт в схеме knowledge — схема нужна до первой миграции
    connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
    connection.commit()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=SCHEMA,
        include_schemas=True,
        include_name=include_name,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = make_engine(postgres_settings(), migrations=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=postgres_settings().url,
        target_metadata=target_metadata,
        literal_binds=True,
        version_table_schema=SCHEMA,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_async_migrations())
