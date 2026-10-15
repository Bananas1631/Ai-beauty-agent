"""Миграции: применяются на чистой базе, откатываются, применяются снова и совпадают с моделями."""

import asyncio

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import inspect

from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine
from beauty_agent.db.models import Base


async def table_names(settings: PostgresSettings) -> set[str]:
    engine = make_engine(settings)
    try:
        async with engine.connect() as connection:
            names = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    finally:
        await engine.dispose()
    return set(names)


async def test_downgrade_and_upgrade_again(alembic_config: Config, migrated: PostgresSettings) -> None:
    # env.py сам вызывает asyncio.run, а внутри работающего цикла событий это запрещено:
    # команды Alembic запускаем в отдельном потоке
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    assert await table_names(migrated) == {"alembic_version"}

    await asyncio.to_thread(command.upgrade, alembic_config, "head")
    assert await table_names(migrated) == {"alembic_version", *Base.metadata.tables}


async def test_models_match_migrations(migrated: PostgresSettings) -> None:
    """То же, что alembic check: autogenerate не видит разницы между моделями и базой."""
    engine = make_engine(migrated)
    try:
        async with engine.connect() as connection:
            diff = await connection.run_sync(
                lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
            )
    finally:
        await engine.dispose()

    assert diff == []
