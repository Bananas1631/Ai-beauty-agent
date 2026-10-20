"""Миграции индексатора совпадают с моделями и откатываются — как alembic check у основного сервиса."""

import asyncio
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Connection, inspect

from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine
from beauty_indexer.db import SCHEMA, Base, include_name


def _diff(connection: Connection) -> list[Any]:
    context = MigrationContext.configure(
        connection,
        opts={"include_schemas": True, "include_name": include_name, "version_table_schema": SCHEMA},
    )
    return list(compare_metadata(context, Base.metadata))


async def test_models_match_migrations(migrated: PostgresSettings) -> None:
    engine = make_engine(migrated)
    try:
        async with engine.connect() as connection:
            diff = await connection.run_sync(_diff)
    finally:
        await engine.dispose()

    assert diff == []


def _tables(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names(schema=SCHEMA))


async def test_downgrade_and_upgrade_again(migrated: PostgresSettings, alembic_config: Config) -> None:
    # env.py сам вызывает asyncio.run — команды Alembic запускаем в отдельном потоке
    await asyncio.to_thread(command.downgrade, alembic_config, "base")
    engine = make_engine(migrated)
    try:
        async with engine.connect() as connection:
            assert await connection.run_sync(_tables) == {"alembic_version"}
        await asyncio.to_thread(command.upgrade, alembic_config, "head")
        async with engine.connect() as connection:
            assert await connection.run_sync(_tables) >= {"documents", "chunks", "reindex_jobs", "event_outbox"}
    finally:
        await engine.dispose()
