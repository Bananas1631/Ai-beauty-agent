import asyncio
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fakeredis import FakeAsyncRedis

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.domain.booking_engine.memory import MemoryLedger
from beauty_agent.domain.booking_engine.offers import PartOfDay
from beauty_agent.domain.booking_engine.service import BookingEntry, BookingService, Outcome
from beauty_agent.domain.entities import ClientContact
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.errors import BookingInProgressError, OfferExpiredError

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=MSK)
DAY = date(2026, 10, 20)
WEEK = DAY + timedelta(days=6)
CLIENT = ClientContact("Анна", "+79000000001")
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


async def test_propose_gives_up_to_three_numbered_offers(engine: BookingService) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    assert [offer.number for offer in offers] == [1, 2, 3]
    assert offers == sorted(offers, key=lambda offer: offer.slot.starts_at)
    assert offers[0].service_name == "Маникюр с покрытием гель-лак"
    assert offers[0].price_text == "от 2 500 до 3 000 ₽"
    assert {offer.master_name for offer in offers} <= {"Ольга", "Марина"}
    parts = {(offer.slot.starts_at.date(), PartOfDay.of(offer.slot.starts_at.astimezone(MSK))) for offer in offers}
    assert len(parts) >= 2  # не три окна подряд в одно утро


async def test_propose_prefers_master(engine: BookingService) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK, preferred_master_id="12")

    assert any(offer.slot.master_id == "12" for offer in offers)


async def test_propose_part_of_day(engine: BookingService) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK, part_of_day=PartOfDay.EVENING)

    assert all(offer.slot.starts_at.astimezone(MSK).hour >= 17 for offer in offers)


async def test_slots_are_cached(engine: BookingService, crm: FakeCRM) -> None:
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    assert crm.calls.count("find_slots") == 1
    assert crm.calls.count("list_services") == 1


async def test_confirm_creates_booking(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    outcome = await engine.confirm(DIALOG, 2, CLIENT)

    assert outcome.status is Outcome.CONFIRMED
    assert outcome.offer == offers[1]
    assert outcome.external_id is not None
    assert len(crm.bookings) == 1
    [entry] = ledger.entries
    assert entry.status is BookingStatus.CONFIRMED
    assert entry.slot == offers[1].slot
    assert len(entry.idempotency_key) == 64
    assert entry.price is None  # у услуги диапазон цен: точную назовёт мастер


async def test_confirm_invalidates_slot_cache(engine: BookingService, crm: FakeCRM) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)
    await engine.confirm(DIALOG, 1, CLIENT)

    again = await engine.propose("dialog-2", "102", date_from=DAY, date_to=WEEK)

    assert crm.calls.count("find_slots") == 2
    assert offers[0].slot not in [offer.slot for offer in again]


async def test_stale_option_is_rejected(engine: BookingService) -> None:
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    with pytest.raises(OfferExpiredError):
        await engine.confirm(DIALOG, 7, CLIENT)
    with pytest.raises(OfferExpiredError):
        await engine.confirm("other-dialog", 1, CLIENT)  # варианты чужого диалога не видны


async def test_double_confirm_gives_one_booking(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    first = await engine.confirm(DIALOG, 1, CLIENT)
    second = await engine.confirm(DIALOG, 1, CLIENT)  # клиент ответил «да» дважды

    assert first == second
    assert len(crm.bookings) == 1
    assert len(ledger.entries) == 1


async def test_parallel_confirm_gives_one_booking(engine: BookingService, crm: FakeCRM) -> None:
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    results = await asyncio.gather(
        engine.confirm(DIALOG, 1, CLIENT), engine.confirm(DIALOG, 1, CLIENT), return_exceptions=True
    )

    assert len(crm.bookings) == 1
    assert sum(isinstance(result, BookingInProgressError) for result in results) <= 1


async def test_taken_slot_gives_new_offers(engine: BookingService, crm: FakeCRM, ledger: MemoryLedger) -> None:
    offers = await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)
    crm.block(offers[0].slot)  # пока клиент думал, время заняли

    outcome = await engine.confirm(DIALOG, 1, CLIENT)

    assert outcome.status is Outcome.SLOT_TAKEN
    assert outcome.alternatives
    assert offers[0].slot not in [offer.slot for offer in outcome.alternatives]
    assert crm.bookings == {}
    assert ledger.entries == []
    # новые варианты сохранены под теми же номерами: «давайте первый» относится к ним
    retry = await engine.confirm(DIALOG, 1, CLIENT)
    assert retry.status is Outcome.CONFIRMED
    assert retry.offer.slot == outcome.alternatives[0].slot


async def test_unavailable_crm_leaves_booking_for_admin(
    engine: BookingService, crm: FakeCRM, ledger: MemoryLedger
) -> None:
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)
    crm.fail_next(1)

    outcome = await engine.confirm(DIALOG, 1, CLIENT)

    assert outcome.status is Outcome.PENDING_ADMIN
    assert crm.bookings == {}
    [entry] = ledger.entries
    assert entry.status is BookingStatus.PENDING
    assert entry.external_id is None


async def test_ledger_failure_does_not_allow_second_booking(engine: BookingService, crm: FakeCRM) -> None:
    class BrokenLedger(MemoryLedger):
        async def record(self, entry: BookingEntry) -> None:
            raise RuntimeError("БД недоступна")

    engine.ledger = BrokenLedger()
    await engine.propose(DIALOG, "102", date_from=DAY, date_to=WEEK)

    first = await engine.confirm(DIALOG, 1, CLIENT)
    second = await engine.confirm(DIALOG, 1, CLIENT)

    assert first.status is Outcome.CONFIRMED
    assert second == first
    assert len(crm.bookings) == 1
