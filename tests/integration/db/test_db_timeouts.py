"""Таймауты на стороне PostgreSQL: зависший запрос и забытая транзакция не держат соединение вечно."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine


async def test_app_engine_has_timeouts(migrated: PostgresSettings) -> None:
    engine = make_engine(migrated.model_copy(update={"statement_timeout_ms": 300}))
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "300ms"
            assert (await connection.execute(text("SHOW idle_in_transaction_session_timeout"))).scalar_one() == "1min"
            with pytest.raises(DBAPIError, match="statement timeout"):
                await connection.execute(text("SELECT pg_sleep(2)"))
    finally:
        await engine.dispose()


async def test_migrations_engine_has_no_statement_timeout(migrated: PostgresSettings) -> None:
    engine = make_engine(migrated, migrations=True)  # CREATE INDEX CONCURRENTLY на большой таблице — минуты
    try:
        async with engine.connect() as connection:
            assert (await connection.execute(text("SHOW statement_timeout"))).scalar_one() == "0"
    finally:
        await engine.dispose()
