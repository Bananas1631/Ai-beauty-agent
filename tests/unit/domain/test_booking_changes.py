"""«Мои записи», отмена и перенос в движке записи: фейковый CRM, Redis в памяти, журнал в памяти."""

import uuid
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.domain.booking_engine.memory import MemoryLedger
from beauty_agent.domain.booking_engine.service import BookingService, Outcome
from beauty_agent.domain.entities import ClientContact, Slot
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.errors import DomainValidationError, OfferExpiredError

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=MSK)
DAY = date(2026, 10, 20)
WEEK = DAY + timedelta(days=6)
CLIENT = ClientContact("Анна", "+79000000001")
CLIENT_ID = uuid.uuid4()
DIALOG = "dialog-1"


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def crm() -> FakeCRM:
    return FakeCRM(clock=lambda: NOW)


@pytest.fixture
def ledger() -> MemoryLedger:
    return MemoryLedger()


@pytest.fixture
def engine(crm: FakeCRM, redis: FakeAsyncRedis, ledger: MemoryLedger) -> BookingService:
    return BookingService(salon_id="salon-1", timezone="Europe/Moscow", provider=crm, redis=redis, ledger=ledger)


async def booked(engine: BookingService, service_id: str = "101") -> str:
    await engine.propose(DIALOG, service_id, date_from=DAY, date_to=WEEK)
    outcome = await engine.confirm(DIALOG, 1, CLIENT, client_id=CLIENT_ID)
    assert outcome.external_id is not None
    return outcome.external_id


async def test_my_bookings_lists_only_own_future_bookings(engine: BookingService, ledger: MemoryLedger) -> None:
    await booked(engine)

    mine = await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    strangers = await engine.client_bookings(DIALOG, uuid.uuid4(), NOW)
    later = await engine.client_bookings(DIALOG, CLIENT_ID, NOW + timedelta(days=30))

    assert [(b.number, b.service_name) for b in mine] == [(1, "Маникюр без покрытия")]
    assert mine[0].master_name in {"Ольга", "Марина"}
    assert strangers == []  # чужие записи не видны
    assert later == []  # прошедшие не показываются
    assert ledger.entries[0].client == CLIENT  # имя и телефон — для карточки клиента


async def test_cancel_once(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    external_id = await booked(engine)
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)

    first = await engine.cancel(DIALOG, 1, NOW)
    second = await engine.cancel(DIALOG, 1, NOW)  # повторное «да, отменяйте»

    assert first.status is Outcome.CANCELLED
    assert second == first
    assert crm.calls.count("cancel_booking") == 1
    assert external_id in crm.cancelled
    assert ledger.entries[0].status is BookingStatus.CANCELLED
    assert await engine.client_bookings(DIALOG, CLIENT_ID, NOW) == []


async def test_reschedule_to_offered_time(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    external_id = await booked(engine)
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    offers = await engine.propose(DIALOG, "101", date_from=DAY + timedelta(days=2), date_to=WEEK)

    outcome = await engine.reschedule(DIALOG, 1, 2, NOW)

    assert outcome.status is Outcome.RESCHEDULED
    assert outcome.new_offer == offers[1]
    assert crm.moved[external_id] == offers[1].slot
    assert ledger.entries[0].slot == offers[1].slot
    [moved] = await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    assert moved.slot == offers[1].slot


async def test_reschedule_only_to_same_service(engine: BookingService) -> None:
    await booked(engine)
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    await engine.propose(DIALOG, "201", date_from=DAY, date_to=WEEK)  # стрижка вместо маникюра

    with pytest.raises(DomainValidationError, match="ту же услугу"):
        await engine.reschedule(DIALOG, 1, 1, NOW)


async def test_reschedule_to_taken_time_keeps_booking(engine: BookingService, crm: FakeCRM) -> None:
    external_id = await booked(engine)
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    offers = await engine.propose(DIALOG, "101", date_from=DAY + timedelta(days=2), date_to=WEEK)
    crm.block(offers[0].slot)  # пока клиент думал, время заняли

    outcome = await engine.reschedule(DIALOG, 1, 1, NOW)

    assert outcome.status is Outcome.SLOT_TAKEN
    assert outcome.alternatives
    assert external_id not in crm.moved


async def test_changes_made_in_crm_are_visible(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    first = await booked(engine)
    second = await booked(engine, "103")
    new_slot = Slot("12", "103", datetime(2026, 10, 23, 18, 0, tzinfo=MSK), 30)
    crm.cancelled.add(first)  # администратор отменил в YCLIENTS
    crm.moved[second] = new_slot  # и перенёс вторую

    bookings = await engine.client_bookings(DIALOG, CLIENT_ID, NOW)

    assert [booking.external_id for booking in bookings] == [second]
    assert bookings[0].slot == new_slot
    statuses = {entry.external_id: (entry.status, entry.slot) for entry in ledger.entries}
    assert statuses[first][0] is BookingStatus.CANCELLED
    assert statuses[second][1] == new_slot


async def test_crm_unavailable_goes_to_admin(engine: BookingService, crm: FakeCRM) -> None:
    await booked(engine)
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    crm.fail_next()

    outcome = await engine.cancel(DIALOG, 1, NOW)
    retry = await engine.cancel(DIALOG, 1, NOW)  # CRM снова доступна — ключ не занят, повтор возможен

    assert outcome.status is Outcome.PENDING_ADMIN
    assert retry.status is Outcome.CANCELLED


async def test_stale_list_and_past_booking(engine: BookingService) -> None:
    await booked(engine)

    with pytest.raises(OfferExpiredError):
        await engine.cancel(DIALOG, 1, NOW)  # список «мои записи» ещё не показывали
    await engine.client_bookings(DIALOG, CLIENT_ID, NOW)
    with pytest.raises(DomainValidationError, match="прошла"):
        await engine.cancel(DIALOG, 1, NOW + timedelta(days=30))
