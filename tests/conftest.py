"""Общие фикстуры для всех тестов."""

import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

import beauty_agent.db
from beauty_agent.api.deps import get_session
from beauty_agent.api.main import create_app
from beauty_agent.core.config import AppSettings, PostgresSettings, RedisSettings, Settings
from beauty_agent.core.db import make_engine
from beauty_agent.core.redis import make_redis
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


# ---------------------------------------------------------------- PostgreSQL и Redis
# Контейнеры стартуют, только когда тест запрашивает базу; нужен запущенный Docker Desktop.


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
def redis_settings() -> Iterator[RedisSettings]:
    with RedisContainer("redis:8-alpine") as container:
        yield RedisSettings(host=container.get_container_host_ip(), port=int(container.get_exposed_port(6379)))


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
    """Сессия внутри внешней транзакции, которая откатывается после теста.

    commit() в коде и тестах фиксирует только точку сохранения (SAVEPOINT), поэтому данные
    одного теста не видны другим, а ограничения базы срабатывают как обычно.
    """
    engine = make_engine(migrated)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                yield session
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.fixture
async def redis(redis_settings: RedisSettings) -> AsyncIterator[Redis]:
    client = make_redis(redis_settings)
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


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


# ---------------------------------------------------------------- приложение


@pytest.fixture
def settings(migrated: PostgresSettings, redis_settings: RedisSettings) -> Settings:
    return Settings(
        app=AppSettings(env="test", log_json=True, ready_timeout_s=1.0),
        postgres=migrated,
        redis=redis_settings,
    )


@pytest.fixture
async def app(settings: Settings, session: AsyncSession) -> AsyncIterator[FastAPI]:
    """Приложение с настоящим lifespan; сессия БД подменена тестовой с откатом."""
    application = create_app(settings)

    async def test_session() -> AsyncIterator[AsyncSession]:
        yield session

    application.dependency_overrides[get_session] = test_session
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    # raise_app_exceptions=False: ошибка 500 приходит ответом, как у настоящего клиента
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http
