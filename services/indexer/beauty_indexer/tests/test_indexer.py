"""Индексатор целиком: событие → задача → обход сайта → knowledge.updated; поиск по REST с OAuth2."""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.jwks import JwksVerifier
from beauty_agent.core.security import Security, generate_signing_key
from beauty_agent.domain.events import KnowledgeUpdated, SiteReindexRequested, SiteReindexRequestedData, parse_event
from beauty_agent.rag.embeddings import HashingEmbeddings
from beauty_indexer.db import Chunk, EventOutbox, JobStatus, ReindexJob
from beauty_indexer.jobs import JobRunner, ReindexRequests, claim
from beauty_indexer.tests.conftest import FIRST_KEY, Issuer, issuer_security
from evals.site_server import site_client, site_host

SITE = f"https://{site_host('lavanda')}/"


def request_event(salon_id: uuid.UUID, *, job_id: uuid.UUID | None = None, site: str = SITE) -> SiteReindexRequested:
    return SiteReindexRequested(salon_id=salon_id, data=SiteReindexRequestedData(site_url=site, job_id=job_id))


async def accept(session_factory: async_sessionmaker[AsyncSession], event: SiteReindexRequested) -> bool:
    """То, что делает потребитель: отметка о событии и задача — одной транзакцией."""
    handler = ReindexRequests()
    async with session_factory() as session, session.begin():
        if not await claim(session, handler.group, event):
            return False
        await handler.handle(session, event)
    return True


async def index_lavanda(session_factory: async_sessionmaker[AsyncSession], salon_id: uuid.UUID) -> uuid.UUID:
    job_id = uuid.uuid4()
    await accept(session_factory, request_event(salon_id, job_id=job_id))
    async with site_client("lavanda") as site:
        runner = JobRunner(session_factory, HashingEmbeddings(), pause_s=0, client=site)
        assert await runner.run_one() is not None
    return job_id


def bearer(
    security: Security, *, scopes: tuple[str, ...] = ("knowledge:read",), salon_id: uuid.UUID | None = None
) -> dict[str, str]:
    token = security.issue_access_token(subject="beauty-agent", scopes=scopes, salon_id=salon_id)
    return {"Authorization": f"Bearer {token}"}


async def test_repeated_event_creates_one_job(session_factory: async_sessionmaker[AsyncSession]) -> None:
    event = request_event(uuid.uuid4())

    assert await accept(session_factory, event)
    assert not await accept(session_factory, event)  # повторная доставка из Kafka

    async with session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(ReindexJob)) == 1


async def test_job_indexes_site_and_publishes_knowledge_updated(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    salon_id = uuid.uuid4()

    job_id = await index_lavanda(session_factory, salon_id)

    async with session_factory() as session:
        chunks = await session.scalar(select(func.count()).select_from(Chunk).where(Chunk.salon_id == salon_id))
        job = await session.scalar(select(ReindexJob).where(ReindexJob.salon_id == salon_id))
        outbox = list(await session.scalars(select(EventOutbox).where(EventOutbox.salon_id == salon_id)))
    assert chunks
    assert job is not None
    assert job.status == JobStatus.DONE.value
    assert len(outbox) == 1
    assert outbox[0].topic == "beauty.knowledge"
    event = parse_event(json.dumps(outbox[0].payload))
    assert isinstance(event, KnowledgeUpdated)
    assert event.data.status == "done"
    assert event.data.job_id == job_id
    assert event.data.pages_indexed == 5
    assert event.data.version is not None
    assert event.data.version.startswith(f"{chunks}:")


async def test_unchanged_site_is_not_reindexed(session_factory: async_sessionmaker[AsyncSession]) -> None:
    salon_id = uuid.uuid4()
    await index_lavanda(session_factory, salon_id)

    await index_lavanda(session_factory, salon_id)

    async with session_factory() as session:
        rows = list(
            await session.scalars(select(EventOutbox).where(EventOutbox.salon_id == salon_id).order_by(EventOutbox.id))
        )
    first, second = (row.payload["data"] for row in rows)
    assert second["pages_unchanged"] == 5
    assert second["pages_indexed"] == 0
    assert second["version"] == first["version"]  # кэш ответов агента остаётся в силе


async def test_unsafe_site_fails_with_reason(session_factory: async_sessionmaker[AsyncSession]) -> None:
    salon_id = uuid.uuid4()
    await accept(session_factory, request_event(salon_id, site="http://127.0.0.1:6379/"))

    await JobRunner(session_factory, HashingEmbeddings(), pause_s=0).run_one()

    async with session_factory() as session:
        job = await session.scalar(select(ReindexJob).where(ReindexJob.salon_id == salon_id))
        row = await session.scalar(select(EventOutbox).where(EventOutbox.salon_id == salon_id))
    assert job is not None
    assert job.status == JobStatus.FAILED.value
    assert row is not None
    assert row.payload["data"]["status"] == "failed"
    assert row.payload["data"]["error"]


async def test_running_salon_is_skipped_and_stale_job_is_taken_again(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    salon_id = uuid.uuid4()
    await accept(session_factory, request_event(salon_id))
    await accept(session_factory, request_event(salon_id))
    runner = JobRunner(session_factory, HashingEmbeddings(), pause_s=0)

    first = await runner.take()
    assert first is not None
    assert await runner.take() is None  # салон уже обходят — вторая задача ждёт

    async with session_factory() as session, session.begin():  # воркер «упал» час назад
        await session.execute(
            update(ReindexJob)
            .where(ReindexJob.id == first.id)
            .values(updated_at=datetime.now(UTC) - timedelta(hours=1))
        )
    retaken = await runner.take()
    assert retaken is not None
    assert retaken.id == first.id
    assert retaken.attempts == 2


async def test_search_finds_page_by_exact_word(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], issuer: Issuer
) -> None:
    salon_id = uuid.uuid4()
    await index_lavanda(session_factory, salon_id)

    response = await client.post(
        f"/v1/salons/{salon_id}/search", json={"question": "Сколько стоит Shellac?"}, headers=bearer(issuer.security)
    )

    assert response.status_code == 200
    body = response.json()
    assert not body["version"].startswith("0:")
    assert body["fragments"]
    assert any("shellac" in fragment["content"].lower() for fragment in body["fragments"][:3])
    assert all(fragment["url"].startswith(SITE) for fragment in body["fragments"])


async def test_other_salon_sees_nothing(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession], issuer: Issuer
) -> None:
    await index_lavanda(session_factory, uuid.uuid4())

    response = await client.post(
        f"/v1/salons/{uuid.uuid4()}/search", json={"question": "Shellac"}, headers=bearer(issuer.security)
    )

    assert response.status_code == 200
    assert response.json() == {"version": "0:-", "fragments": []}


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer not-a-jwt"}, {"Authorization": "Basic eDp5"}])
async def test_search_requires_token(client: AsyncClient, headers: dict[str, str]) -> None:
    response = await client.post(f"/v1/salons/{uuid.uuid4()}/search", json={"question": "цены"}, headers=headers)

    assert response.status_code == 401


async def test_token_for_other_service_and_foreign_partner_are_rejected(client: AsyncClient, issuer: Issuer) -> None:
    salon_id = uuid.uuid4()

    other_service = await client.post(
        f"/v1/salons/{salon_id}/search",
        json={"question": "цены"},
        headers=bearer(issuer.security, scopes=("chat:write",)),
    )
    partner = await client.get(f"/v1/salons/{salon_id}/version", headers=bearer(issuer.security, salon_id=uuid.uuid4()))
    own_partner = await client.get(f"/v1/salons/{salon_id}/version", headers=bearer(issuer.security, salon_id=salon_id))

    assert other_service.status_code == 401  # аудитория beauty-agent-api — токен не для индексатора
    assert partner.status_code == 404
    assert own_partner.status_code == 200


async def test_token_signed_by_rotated_key_is_accepted(client: AsyncClient, issuer: Issuer, app: FastAPI) -> None:
    """Ключ сменили после того, как индексатор закэшировал JWKS: незнакомый kid — перечитать JWKS."""
    verifier: JwksVerifier = app.state.verifier
    await verifier.keys()  # в кэше только первый ключ
    verifier.min_refresh_s = 0
    issuer.security = issuer_security(f"{generate_signing_key()},{FIRST_KEY}")  # новый подписывает, старый опубликован

    fresh = await client.get(f"/v1/salons/{uuid.uuid4()}/version", headers=bearer(issuer.security))

    assert fresh.status_code == 200


async def test_ready(client: AsyncClient) -> None:
    assert (await client.get("/ready")).json() == {"status": "ok", "postgres": "ok"}
    assert (await client.get("/health")).status_code == 200
