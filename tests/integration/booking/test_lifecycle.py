"""Жизнь записи после создания: напоминания, перенос и отмена в YCLIENTS, вебхук YCLIENTS."""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import SECURITY, TOKEN, RecordingQueue, RecordingSender

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.core.config import YclientsSettings
from beauty_agent.core.logging import mask_text
from beauty_agent.core.security import Security
from beauty_agent.db.models import Booking, Client, Conversation, Reminder, Salon
from beauty_agent.db.repositories.bookings import SqlBookingLedger
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.booking_engine.service import BookingEntry
from beauty_agent.domain.entities import BookingRequest, ClientContact, Slot
from beauty_agent.domain.enums import BookingStatus, Channel, CrmKind, ReminderStatus
from beauty_agent.workers.reminders import ReminderSender, refresh_booking

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 11, 23, 9, 0, tzinfo=MSK)  # понедельник
DAY = date(2026, 11, 24)
CLIENT = ClientContact("Анна", "+79000000001")


@dataclass
class Env:
    salon: Salon
    crm: FakeCRM
    registry: ProviderRegistry
    sender: RecordingSender
    outbox: Outbox
    conversation: Conversation
    client_id: uuid.UUID


@pytest.fixture
async def env(session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon) -> Env:
    return await make_env_with(session, session_factory, salon)


async def make_env_with(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    *,
    messenger: bool = True,
) -> Env:
    salon.settings = {"crm_kind": CrmKind.FAKE.value, "reminders_enabled": True}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    account = await ChannelAccountsRepository(session).connect(
        salon.id, Channel.TELEGRAM, f"bot-{uuid.uuid4().hex[:6]}", {"token": TOKEN}, SECURITY
    )
    client = Client(salon_id=salon.id)
    session.add(client)
    await session.flush()
    conversation = Conversation(
        salon_id=salon.id,
        client_id=client.id,
        channel=Channel.TELEGRAM if messenger else Channel.WEB,
        reply_route={"account_id": str(account.id), "chat_id": "555", "route": {}} if messenger else None,
    )
    session.add(conversation)
    await session.commit()
    crm = FakeCRM(clock=lambda: NOW)
    registry = ProviderRegistry(YclientsSettings())
    registry.override(salon, crm)
    sender = RecordingSender()
    outbox = Outbox(session_factory, SECURITY, {Channel.TELEGRAM: sender})
    return Env(salon, crm, registry, sender, outbox, conversation, client.id)


async def book(session: AsyncSession, env: Env, *, after_hour: int = 12) -> tuple[Slot, str]:
    """Запись в фейковом CRM и в нашей БД, как после инструмента book."""
    slots = await env.crm.find_slots("101", DAY, DAY)
    slot = next(item for item in slots if item.starts_at.astimezone(MSK).hour >= after_hour)
    result = await env.crm.create_booking(BookingRequest(slot, CLIENT, uuid.uuid4().hex))
    ledger = SqlBookingLedger(session, env.salon.id, timezone=env.salon.timezone, reminders=True, clock=lambda: NOW)
    await ledger.record(
        BookingEntry(
            conversation_id=str(env.conversation.id),
            client_id=env.client_id,
            slot=slot,
            status=BookingStatus.CONFIRMED,
            idempotency_key=uuid.uuid4().hex,
            external_id=result.external_id,
            price=None,
            client=CLIENT,
        )
    )
    return slot, result.external_id


def reminder_sender(session_factory: async_sessionmaker[AsyncSession], env: Env, moment: datetime) -> ReminderSender:
    return ReminderSender(session_factory, env.registry, env.outbox, clock=lambda: moment)


async def reminders(session: AsyncSession, salon: Salon) -> dict[str, tuple[ReminderStatus, datetime]]:
    rows = (await session.scalars(select(Reminder).where(Reminder.salon_id == salon.id))).all()
    for row in rows:
        await session.refresh(row)
    return {row.kind: (row.status, row.due_at) for row in rows}


async def test_booking_plans_reminders_and_fills_client_card(session: AsyncSession, env: Env) -> None:
    slot, _ = await book(session, env)

    planned = await reminders(session, env.salon)

    assert planned == {
        "24h": (ReminderStatus.PENDING, slot.starts_at - timedelta(hours=24)),
        "2h": (ReminderStatus.PENDING, slot.starts_at - timedelta(hours=2)),
    }
    client = await session.get(Client, env.client_id, populate_existing=True)
    assert client is not None
    assert (client.name, client.phone) == ("Анна", "+79000000001")


async def test_reminder_is_sent_once_to_client_channel(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], env: Env
) -> None:
    slot, _ = await book(session, env)
    due = slot.starts_at - timedelta(hours=24)

    first = await reminder_sender(session_factory, env, due).send_due()
    again = await reminder_sender(session_factory, env, due + timedelta(minutes=5)).send_due()

    assert (first.sent, again.sent) == (1, 0)
    [delivery] = env.sender.sent
    assert delivery.chat_id == "555"
    assert "Маникюр без покрытия" in delivery.text
    assert slot.starts_at.astimezone(MSK).strftime("%d.%m в %H:%M") in delivery.text
    assert (await reminders(session, env.salon))["24h"][0] is ReminderStatus.SENT


async def test_cancelled_in_crm_is_not_reminded(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], env: Env
) -> None:
    slot, external_id = await book(session, env)
    env.crm.cancelled.add(external_id)  # администратор отменил запись прямо в YCLIENTS

    report = await reminder_sender(session_factory, env, slot.starts_at - timedelta(hours=24)).send_due()

    assert (report.sent, report.cancelled) == (0, 1)
    assert env.sender.sent == []
    booking = await session.scalar(select(Booking).where(Booking.external_id == external_id))
    assert booking is not None
    await session.refresh(booking)
    assert booking.status is BookingStatus.CANCELLED
    assert {status for status, _ in (await reminders(session, env.salon)).values()} == {ReminderStatus.CANCELLED}


async def test_moved_in_crm_reschedules_reminders(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], env: Env
) -> None:
    slot, external_id = await book(session, env)
    later = Slot(slot.master_id, slot.service_id, slot.starts_at + timedelta(days=2), slot.duration_min)
    env.crm.moved[external_id] = later

    report = await reminder_sender(session_factory, env, slot.starts_at - timedelta(hours=24)).send_due()

    assert (report.sent, report.cancelled) == (0, 1)
    planned = await reminders(session, env.salon)
    assert planned["24h"] == (ReminderStatus.PENDING, later.starts_at - timedelta(hours=24))
    booking = await session.scalar(select(Booking).where(Booking.external_id == external_id))
    assert booking is not None
    await session.refresh(booking)
    assert booking.starts_at == later.starts_at


async def test_crm_unavailable_retries_later(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], env: Env
) -> None:
    slot, _ = await book(session, env)
    env.crm.fail_next()

    report = await reminder_sender(session_factory, env, slot.starts_at - timedelta(hours=24)).send_due()
    retried = await reminder_sender(session_factory, env, slot.starts_at - timedelta(hours=23)).send_due()

    assert (report.retry, report.sent) == (1, 0)
    assert retried.sent == 1


async def test_web_client_gets_no_reminder(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    web = await make_env_with(session, session_factory, salon, messenger=False)
    slot, _ = await book(session, web)

    report = await reminder_sender(session_factory, web, slot.starts_at - timedelta(hours=24)).send_due()

    assert (report.sent, report.skipped) == (0, 1)


async def test_refresh_booking_from_crm(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], env: Env
) -> None:
    _, external_id = await book(session, env)

    unchanged = await refresh_booking(session_factory, env.registry, env.salon.id, external_id)
    env.crm.cancelled.add(external_id)
    cancelled = await refresh_booking(session_factory, env.registry, env.salon.id, external_id)
    unknown = await refresh_booking(session_factory, env.registry, env.salon.id, "123456")

    assert (unchanged, cancelled, unknown) == ("unchanged", "cancelled", "unknown")


# ---------------------------------------------------------------- вебхук YCLIENTS


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.runtime.queue = recording
    return recording


async def test_yclients_webhook(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, queue: RecordingQueue
) -> None:
    salon.yclients_company_id = 555000 + int(uuid.uuid4().int % 900)
    await session.commit()
    security: Security = app.state.security
    url = f"/api/v1/webhooks/yclients/{salon.id}/{security.webhook_secret('yclients', salon.id)}"
    event = {"company_id": salon.yclients_company_id, "resource": "record", "resource_id": 900001, "status": "update"}

    forged = await client.post(f"/api/v1/webhooks/yclients/{salon.id}/{'0' * 64}", json=event)
    accepted = await client.post(url, json=event)
    other_company = await client.post(url, json={**event, "company_id": 1})
    not_record = await client.post(url, json={**event, "resource": "client"})
    injected = await client.post(url, json={**event, "resource_id": "1/../2"})

    assert forged.status_code == 401
    assert all(r.status_code == 200 for r in (accepted, other_company, not_record, injected))
    assert queue.synced == [(salon.id, "900001")]  # только ID записи — саму запись перечитает воркер


def test_webhook_secret_is_masked_in_logs() -> None:
    salon_id = uuid.uuid4()
    line = f'"POST /api/v1/webhooks/yclients/{salon_id}/{"ab" * 32} HTTP/1.1" 200'

    assert "ab" * 32 not in mask_text(line)
    assert str(salon_id) in mask_text(line)
