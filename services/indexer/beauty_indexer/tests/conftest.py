"""Фикстуры индексатора: свой PostgreSQL с pgvector и только своими миграциями (схема knowledge).

Индексатор не зависит от схемы основного сервиса — и тесты это подтверждают: таблиц основного
сервиса в этой базе нет. Обход сайта и задачи коммитят по-настоящему, поэтому после каждого теста
таблицы схемы очищаются.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import structlog
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.community.postgres import PostgresContainer

from beauty_agent.core.config import AppSettings, PostgresSettings, SecuritySettings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.security import Security, generate_signing_key
from beauty_agent.rag.embeddings import HashingEmbeddings
from beauty_indexer.api import create_app
from beauty_indexer.config import IndexerSettings, Settings
from beauty_indexer.db import SCHEMA

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"
JWKS_URL = "http://beauty-agent.test/.well-known/jwks.json"
FIRST_KEY = generate_signing_key()


@pytest.fixture(autouse=True)
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture(scope="session")
def postgres() -> Iterator[PostgresSettings]:
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
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    config.attributes["postgres"] = postgres
    return config


@pytest.fixture(scope="session")
def migrated(postgres: PostgresSettings, alembic_config: Config) -> PostgresSettings:
    command.upgrade(alembic_config, "head")
    return postgres


@pytest.fixture
async def session_factory(migrated: PostgresSettings) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(migrated)
    try:
        yield make_session_factory(engine)
    finally:
        names = ("chunks", "documents", "reindex_jobs", "processed_events", "event_outbox")
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {', '.join(f'{SCHEMA}.{name}' for name in names)} CASCADE"))
        await engine.dispose()


def issuer_security(signing_keys: str) -> Security:
    return Security(SecuritySettings(signing_keys=SecretStr(signing_keys)), env="test")


@dataclass
class Issuer:
    """Основной сервис — издатель токенов; JWKS отдаёт ключи его текущей конфигурации."""

    security: Security


@pytest.fixture
def issuer() -> Issuer:
    return Issuer(issuer_security(FIRST_KEY))


def jwks_transport(issuer: Issuer) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url) == JWKS_URL:
            return httpx.Response(200, json=issuer.security.jwks())
        return httpx.Response(404)

    return httpx.MockTransport(handle)


@pytest.fixture
async def app(migrated: PostgresSettings, issuer: Issuer) -> AsyncIterator[FastAPI]:
    settings = Settings(
        app=AppSettings(env="test", log_json=True, ready_timeout_s=1.0),
        postgres=migrated,
        indexer=IndexerSettings(jwks_url=JWKS_URL),
    )
    async with httpx.AsyncClient(transport=jwks_transport(issuer)) as jwks_http:
        application = create_app(settings, embedder=HashingEmbeddings(), http=jwks_http)
        async with application.router.lifespan_context(application):
            yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://indexer.test") as http:
        yield http
