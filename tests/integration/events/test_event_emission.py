"""Доменные события пишутся в event_outbox той же транзакцией, что и данные, и без персональных данных."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import RecordingNotifier
from tests.integration.db.test_booking_ledger import SLOT, catalog  # noqa: F401 — фикстура каталога

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.llm.fake import ScriptedLLM, reply
from beauty_agent.conversation.service import ConversationService, IncomingMessage
from beauty_agent.core.config import YclientsSettings
from beauty_agent.db.models import Booking, Client, EventOutbox, Salon
from beauty_agent.db.repositories.bookings import BookingsRepository, SqlBookingLedger
from beauty_agent.domain.booking_engine.service import BookingEntry
from beauty_agent.domain.entities import ClientContact, Slot
from beauty_agent.domain.enums import BookingStatus, Channel, CrmKind
from beauty_agent.domain.events import MessageReceived, MessageReceivedData, parse_event
from beauty_agent.events.outbox import record_event
from beauty_agent.workers.attribution import attribute_visits

MSK = ZoneInfo("Europe/Moscow")
PHONE = "+79161502239"


async def outbox(session: AsyncSession, salon: Salon) -> list[EventOutbox]:
    rows = await session.scalars(select(EventOutbox).where(EventOutbox.salon_id == salon.id).order_by(EventOutbox.id))
    return list(rows)


@pytest.mark.usefixtures("catalog")
async def test_booking_lifecycle_events(session: AsyncSession, salon: Salon) -> None:
    client = Client(salon_id=salon.id)
    session.add(client)
    await session.commit()
    ledger = SqlBookingLedger(session, salon.id)
    entry = BookingEntry(
        "dialog-1",
        client.id,
        SLOT,
        BookingStatus.CONFIRMED,
        "key-1",
        "900001",
        Decimal(2500),
        ClientContact("Анна", PHONE),
    )

    await ledger.record(entry)
    await ledger.move("900001", Slot("11", "102", SLOT.starts_at + timedelta(days=1), 90))
    await ledger.cancel("900001")
    rows = await outbox(session, salon)

    assert [row.event_type for row in rows] == ["booking.created", "booking.rescheduled", "booking.cancelled"]
    assert {row.topic for row in rows} == {"beauty.bookings"}
    created, moved, cancelled = (parse_event(json.dumps(row.payload)) for row in rows)
    assert created.data.client_id == client.id  # type: ignore[union-attr]
    assert moved.data.previous_starts_at == SLOT.starts_at  # type: ignore[union-attr]
    assert cancelled.data.status is BookingStatus.CANCELLED  # type: ignore[union-attr]
    dumped = json.dumps([row.payload for row in rows], ensure_ascii=False)
    assert PHONE not in dumped
    assert "Анна" not in dumped  # в событиях только ID


async def test_rollback_drops_the_event(session_factory: async_sessionmaker[AsyncSession], salon: Salon) -> None:
    event = MessageReceived(
        salon_id=salon.id,
        data=MessageReceivedData(
            message_id=salon.id, conversation_id=salon.id, client_id=salon.id, channel=Channel.TELEGRAM
        ),
    )
    async with session_factory() as session:
        await record_event(session, event)
        await session.rollback()
    async with session_factory() as session:
        assert await outbox(session, salon) == []


async def test_dialog_turn_records_message_and_llm_events(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], redis: Redis, salon: Salon
) -> None:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    service = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=ProviderRegistry(YclientsSettings()),
        llm_clients={"scripted": ScriptedLLM([reply("Здравствуйте! Чем помочь?")])},
        default_models=["scripted"],
        notifier=RecordingNotifier(),
    )

    await service.handle(IncomingMessage(salon, Channel.WEB, "session-1", f"Привет, мой телефон {PHONE}"))
    rows = await outbox(session, salon)

    assert [row.event_type for row in rows] == ["message.received", "llm.call.finished"]
    llm = parse_event(json.dumps(rows[1].payload))
    assert llm.data.model == "fake"  # type: ignore[union-attr]  # ключ модели ScriptedLLM
    assert llm.data.prompt_tokens > 0  # type: ignore[union-attr]
    assert "Привет" not in json.dumps([row.payload for row in rows], ensure_ascii=False)  # текста нет


@pytest.mark.usefixtures("catalog")
async def test_visit_is_attributed_once(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    ledger = SqlBookingLedger(session, salon.id)
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    await ledger.record(BookingEntry("d", None, SLOT, BookingStatus.CONFIRMED, "past", "1", Decimal(2500)))
    future = Slot("11", "102", now + timedelta(days=2), 90)
    await ledger.record(BookingEntry("d", None, future, BookingStatus.CONFIRMED, "future", "2", Decimal(2500)))
    cancelled_slot = Slot("11", "102", SLOT.starts_at + timedelta(hours=3), 90)
    await ledger.record(BookingEntry("d", None, cancelled_slot, BookingStatus.CONFIRMED, "cancelled", "3", None))
    await ledger.cancel("3")

    first = await attribute_visits(session_factory, now=now)
    again = await attribute_visits(session_factory, now=now + timedelta(minutes=15))
    attributed = [row for row in await outbox(session, salon) if row.event_type == "booking.attributed"]
    past = await BookingsRepository(session, salon.id).by_external_id("1")

    assert (first, again) == (1, 0)  # одна запись и один раз
    assert past is not None
    assert [row.payload["data"]["booking_id"] for row in attributed] == [str(past.id)]
    assert attributed[0].payload["data"]["billing_month"] == "2026-10"
    assert attributed[0].dedupe_key == f"booking.attributed:{past.id}"


@pytest.mark.usefixtures("catalog")
async def test_manual_bookings_are_not_attributed(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    await SqlBookingLedger(session, salon.id).record(
        BookingEntry("d", None, SLOT, BookingStatus.CONFIRMED, "manual", "9", None)
    )
    await session.execute(update(Booking).where(Booking.salon_id == salon.id).values(created_by_ai=False))
    await session.commit()

    assert await attribute_visits(session_factory, now=SLOT.starts_at + timedelta(hours=2)) == 0
