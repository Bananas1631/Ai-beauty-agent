"""Окружение Alembic: к какой базе подключаться и с какими моделями сравнивать её схему.

Этот файл выполняет каждая команда alembic: upgrade, downgrade, revision --autogenerate.
"""

import asyncio
from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Connection

from beauty_agent.core.config import PostgresSettings, get_settings
from beauty_agent.core.db import make_engine
from beauty_agent.db.models import Base

config = context.config

# Логи — из alembic.ini. Тесты запускают миграции без файла настроек и логи не трогают
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# С чем autogenerate сравнивает базу: все таблицы моделей
target_metadata = Base.metadata


def postgres_settings() -> PostgresSettings:
    """Тесты передают настройки своей базы через config.attributes; иначе — из .env."""
    settings = config.attributes.get("postgres")
    return settings if isinstance(settings, PostgresSettings) else get_settings().postgres


# Продолжение — как писать миграцию и как её выполнять:


def render_item(type_: str, obj: Any, _autogen_context: AutogenContext) -> Any:
    """Как autogenerate записывает объект в миграцию: False — как обычно, None — не записывать.

    CHECK перечисления создаёт сам тип колонки (str_enum). Alembic 1.20 не узнаёт такие CHECK
    у SQLAlchemy 2.1 и записывает их ещё раз отдельно — в таблице получились бы дубли.
    """
    if type_ == "check" and obj._type_bound:
        return None
    return False


def run_migrations_offline() -> None:
    """Режим --sql: напечатать SQL миграций, не подключаясь к базе."""
    context.configure(
        url=postgres_settings().url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, render_item=render_item)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Движок — тот же, что у приложения; миграции выполняются в одной транзакции."""
    engine = make_engine(postgres_settings(), migrations=True)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())
