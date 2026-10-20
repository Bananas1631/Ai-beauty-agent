"""Обработчики событий шага 23 без Kafka: запрос переиндексации, результат индексатора, вебхуки партнёрам."""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.webhooks_out import WebhookDeliveryError
from beauty_agent.core.jobs import JobKind, JobStatus, JobStore
from beauty_agent.db.models import EventOutbox, Salon
from beauty_agent.db.repositories.oauth_clients import OAuthClientsRepository
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.events import (
    BookingAttributed,
    BookingAttributedData,
    BookingCreated,
    BookingData,
    KnowledgeUpdated,
    KnowledgeUpdatedData,
)
from beauty_agent.events.handlers.knowledge import EventReindexQueue, KnowledgeSyncHandler, NoSiteError
from beauty_agent.events.handlers.webhooks import PartnerWebhooksHandler
from beauty_agent.rag.remote import site_version_key


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def reindex_site(self, salon_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.calls.append((salon_id, job_id))


class RecordingSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, Mapping[str, Any]]] = []
        self.fail = fail

    async def deliver(self, url: str, payload: Mapping[str, Any]) -> None:
        if self.fail:
            raise WebhookDeliveryError("HTTP 500")
        self.sent.append((url, payload))


def booking_created(salon_id: uuid.UUID) -> BookingCreated:
    start = datetime(2027, 1, 10, 12, tzinfo=UTC)
    return BookingCreated(
        salon_id=salon_id,
        data=BookingData(
            booking_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            master_id=uuid.uuid4(),
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            status=BookingStatus.CONFIRMED,
            created_by_ai=True,
        ),
    )


async def outbox_events(session: AsyncSession, salon: Salon) -> list[EventOutbox]:
    return list(await session.scalars(select(EventOutbox).where(EventOutbox.salon_id == salon.id)))


async def test_indexer_mode_publishes_request_with_job(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    salon.settings = {**salon.settings, "site_url": "https://lavanda.example/"}
    await session.commit()
    job_id = uuid.uuid4()

    await EventReindexQueue(session_factory).reindex_site(salon.id, job_id)

    (row,) = await outbox_events(session, salon)
    assert row.event_type == "site.reindex.requested"
    assert row.topic == "beauty.knowledge"
    assert row.payload["data"] == {"site_url": "https://lavanda.example/", "job_id": str(job_id), "max_pages": 30}


async def test_shadow_mode_runs_local_and_sends_request_without_job(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    salon.settings = {**salon.settings, "site_url": "https://lavanda.example/"}
    await session.commit()
    local = RecordingQueue()
    job_id = uuid.uuid4()

    await EventReindexQueue(session_factory, local=local).reindex_site(salon.id, job_id)

    (row,) = await outbox_events(session, salon)
    assert row.payload["data"]["job_id"] is None  # задачу ведёт свой обход, индексатор только сравнивается
    assert local.calls == [(salon.id, job_id)]


async def test_no_site_is_refused(session_factory: async_sessionmaker[AsyncSession], salon: Salon) -> None:
    with pytest.raises(NoSiteError):
        await EventReindexQueue(session_factory).reindex_site(salon.id, uuid.uuid4())


async def test_knowledge_updated_sets_version_and_finishes_job(
    session: AsyncSession, salon: Salon, redis: Redis
) -> None:
    jobs = JobStore(redis)
    job, _ = await jobs.create(salon.id, JobKind.REINDEX)
    event = KnowledgeUpdated(
        salon_id=salon.id,
        data=KnowledgeUpdatedData(status="done", version="42:2026", job_id=job.id, pages_seen=5, pages_indexed=5),
    )

    await KnowledgeSyncHandler(redis).handle(session, event)

    assert await redis.get(site_version_key(salon.id)) == "42:2026"
    finished = await jobs.get(job.id)
    assert finished is not None
    assert finished.status is JobStatus.DONE
    assert finished.result["pages_indexed"] == 5


async def test_failed_reindex_fails_job_and_keeps_version(session: AsyncSession, salon: Salon, redis: Redis) -> None:
    jobs = JobStore(redis)
    job, _ = await jobs.create(salon.id, JobKind.REINDEX)
    await redis.set(site_version_key(salon.id), "old")
    event = KnowledgeUpdated(
        salon_id=salon.id, data=KnowledgeUpdatedData(status="failed", job_id=job.id, error="Адрес сайта недопустим")
    )

    await KnowledgeSyncHandler(redis).handle(session, event)

    failed = await jobs.get(job.id)
    assert failed is not None
    assert failed.status is JobStatus.FAILED
    assert failed.error == "Адрес сайта недопустим"
    assert await redis.get(site_version_key(salon.id)) == "old"


async def test_webhooks_go_only_to_partners_of_the_salon(
    session: AsyncSession, salon: Salon, other_salon: Salon
) -> None:
    clients = OAuthClientsRepository(session)
    await clients.create(name="CRM", scopes=["chat:write"], salon_id=salon.id, webhook_url="https://crm.example/hook")
    await clients.create(name="Без вебхука", scopes=["chat:write"], salon_id=salon.id)
    await clients.create(
        name="Чужой", scopes=["chat:write"], salon_id=other_salon.id, webhook_url="https://other.example/hook"
    )
    off, _ = await clients.create(
        name="Отключён", scopes=["chat:write"], salon_id=salon.id, webhook_url="https://off.example/hook"
    )
    await clients.deactivate(off.client_id)
    await session.commit()
    sender = RecordingSender()
    event = booking_created(salon.id)

    await PartnerWebhooksHandler(sender).handle(session, event)

    assert [url for url, _ in sender.sent] == ["https://crm.example/hook"]
    assert sender.sent[0][1]["event_id"] == str(event.event_id)


async def test_attribution_is_not_sent_and_failure_raises(session: AsyncSession, salon: Salon) -> None:
    await OAuthClientsRepository(session).create(
        name="CRM", scopes=["chat:write"], salon_id=salon.id, webhook_url="https://crm.example/hook"
    )
    await session.commit()
    attributed = BookingAttributed(
        salon_id=salon.id,
        data=BookingAttributedData(
            booking_id=uuid.uuid4(),
            starts_at=datetime(2027, 1, 10, tzinfo=UTC),
            status=BookingStatus.COMPLETED,
            billing_month="2027-01",
        ),
    )
    quiet = RecordingSender()
    await PartnerWebhooksHandler(quiet).handle(session, attributed)
    assert quiet.sent == []

    with pytest.raises(WebhookDeliveryError):  # потребитель откатит отметку и повторит, потом — DLQ
        await PartnerWebhooksHandler(RecordingSender(fail=True)).handle(session, booking_created(salon.id))
