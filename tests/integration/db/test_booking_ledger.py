"""Сохранение записи движка в PostgreSQL: связь с каталогом, повтор ключа, пересечение."""

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Master, Salon, Service
from beauty_agent.db.repositories.bookings import BookingsRepository, SqlBookingLedger
from beauty_agent.domain.booking_engine.service import BookingEntry
from beauty_agent.domain.entities import Slot
from beauty_agent.domain.enums import BookingStatus

MSK = ZoneInfo("Europe/Moscow")
SLOT = Slot("11", "102", datetime(2026, 10, 20, 10, 0, tzinfo=MSK), 90)


@pytest.fixture
async def catalog(session: AsyncSession, salon: Salon) -> None:
    session.add_all(
        [
            Master(salon_id=salon.id, external_id="11", name="Ольга"),
            Service(
                salon_id=salon.id,
                external_id="102",
                name="Маникюр с покрытием",
                price_min=Decimal(2500),
                price_max=Decimal(3000),
                duration_min=90,
            ),
        ]
    )
    await session.commit()


def entry(key: str, *, slot: Slot = SLOT) -> BookingEntry:
    return BookingEntry("dialog-1", None, slot, BookingStatus.CONFIRMED, key, "900001", None)


@pytest.mark.usefixtures("catalog")
async def test_records_booking_linked_to_catalog(session: AsyncSession, salon: Salon) -> None:
    await SqlBookingLedger(session, salon.id).record(entry("k" * 64))

    booking = await BookingsRepository(session, salon.id).by_idempotency_key("k" * 64)
    assert booking is not None
    assert booking.created_by_ai is True
    assert booking.external_id == "900001"
    assert booking.ends_at - booking.starts_at == SLOT.ends_at - SLOT.starts_at


@pytest.mark.usefixtures("catalog")
async def test_same_key_is_recorded_once(session: AsyncSession, salon: Salon) -> None:
    ledger = SqlBookingLedger(session, salon.id)
    await ledger.record(entry("same-key"))
    await ledger.record(entry("same-key"))

    assert await BookingsRepository(session, salon.id).count() == 1


@pytest.mark.usefixtures("catalog")
async def test_overlap_in_db_is_reported(session: AsyncSession, salon: Salon) -> None:
    ledger = SqlBookingLedger(session, salon.id)
    await ledger.record(entry("first"))

    overlapping = Slot("11", "102", datetime(2026, 10, 20, 11, 0, tzinfo=MSK), 90)
    with pytest.raises(IntegrityError):
        await ledger.record(entry("second", slot=overlapping))


async def test_unknown_catalog_is_skipped(session: AsyncSession, salon: Salon) -> None:
    await SqlBookingLedger(session, salon.id).record(entry("no-catalog"))

    assert await BookingsRepository(session, salon.id).count() == 0
