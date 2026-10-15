"""Общие фикстуры для всех тестов."""

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import structlog
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

import beauty_agent.db
from beauty_agent.core.config import PostgresSettings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.db.models import Salon

MIGRATIONS = Path(beauty_agent.db.__file__).parent / "migrations"


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    """configure_logging меняет глобальные настройки — после теста возвращаем как было."""
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------- PostgreSQL
# Контейнер стартует, только когда тест запрашивает базу; нужен запущенный Docker Desktop.


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresSettings]:
    """Чистый PostgreSQL с pgvector — тот же образ, что в docker-compose. Один на все тесты."""
    with PostgresContainer("pgvector/pgvector:pg17") as container:
        yield PostgresSettings(
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            user=container.username,
            password=SecretStr(container.password),
            db=container.dbname,
        )


@pytest.fixture(scope="session")
def alembic_config(postgres: PostgresSettings) -> Config:
    """Alembic без alembic.ini: путь к миграциям и база тестов передаются напрямую."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["postgres"] = postgres  # env.py возьмёт эти настройки вместо .env
    return config


@pytest.fixture(scope="session")
def migrated(postgres: PostgresSettings, alembic_config: Config) -> PostgresSettings:
    """База со схемой из миграций — как на сервере, а не из create_all."""
    command.upgrade(alembic_config, "head")
    return postgres


@pytest.fixture
async def session(migrated: PostgresSettings) -> AsyncIterator[AsyncSession]:
    engine = make_engine(migrated)
    try:
        async with make_session_factory(engine)() as session:
            yield session
    finally:
        await engine.dispose()


async def _new_salon(session: AsyncSession) -> Salon:
    salon = Salon(slug=f"test-{uuid.uuid4().hex[:8]}", name="Тестовый салон")
    session.add(salon)
    await session.commit()
    return salon


@pytest.fixture
async def salon(session: AsyncSession) -> Salon:
    """Новый салон в каждом тесте: данные разных тестов не пересекаются."""
    return await _new_salon(session)


@pytest.fixture
async def other_salon(session: AsyncSession) -> Salon:
    return await _new_salon(session)
