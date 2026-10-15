from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.domain.entities import BookingRequest, ClientContact, Slot
from beauty_agent.domain.errors import NotFoundError, ProviderUnavailableError, SlotTakenError

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 19, 9, 0, tzinfo=MSK)  # понедельник, утро
TUESDAY = date(2026, 10, 20)
CLIENT = ClientContact(name="Анна", phone="+79000000001")


@pytest.fixture
def crm() -> FakeCRM:
    return FakeCRM(clock=lambda: NOW)


async def first_slot(crm: FakeCRM, service_id: str = "102", day: date = TUESDAY) -> Slot:
    slots = await crm.find_slots(service_id, day, day)
    assert slots
    return slots[0]


def test_fake_matches_the_protocol(crm: FakeCRM) -> None:
    provider: BookingProvider = crm  # mypy проверяет, что интерфейс реализован полностью
    assert provider is crm


async def test_catalog(crm: FakeCRM) -> None:
    services = await crm.list_services()
    masters = await crm.list_masters()

    assert len(services) == 12
    assert len(masters) == 4
    assert (await crm.get_salon_info()).timezone == "Europe/Moscow"


async def test_masters_for_service(crm: FakeCRM) -> None:
    masters = await crm.list_masters("102")

    assert {master.name for master in masters} == {"Ольга", "Марина"}


async def test_slots_respect_schedule_and_lead_time(crm: FakeCRM) -> None:
    slots = await crm.find_slots("102", NOW.date(), NOW.date() + timedelta(days=6))

    assert slots == sorted(slots, key=lambda slot: (slot.starts_at, slot.master_id))
    assert all(slot.starts_at >= NOW + timedelta(hours=1) for slot in slots)
    assert all(10 <= slot.starts_at.astimezone(MSK).hour < 20 for slot in slots)
    # Марина не работает в понедельник и вторник
    assert not any(slot.master_id == "12" and slot.starts_at.weekday() in (0, 1) for slot in slots)


async def test_slots_of_one_master(crm: FakeCRM) -> None:
    slots = await crm.find_slots("102", TUESDAY, TUESDAY, master_id="11")

    assert slots
    assert {slot.master_id for slot in slots} == {"11"}


async def test_unknown_service(crm: FakeCRM) -> None:
    with pytest.raises(NotFoundError):
        await crm.find_slots("999", TUESDAY, TUESDAY)


async def test_booking_in_free_time(crm: FakeCRM) -> None:
    slot = await first_slot(crm)

    result = await crm.create_booking(BookingRequest(slot, CLIENT, "key-1"))

    assert result.slot == slot
    assert await crm.find_booking(slot, CLIENT.phone) == result
    assert slot not in await crm.find_slots("102", TUESDAY, TUESDAY)  # время больше не свободно


async def test_booking_in_busy_time_is_rejected(crm: FakeCRM) -> None:
    slot = await first_slot(crm)
    await crm.create_booking(BookingRequest(slot, CLIENT, "key-1"))

    with pytest.raises(SlotTakenError):
        await crm.create_booking(BookingRequest(slot, ClientContact("Вера", "+79000000002"), "key-2"))


async def test_repeat_with_same_key_gives_one_booking(crm: FakeCRM) -> None:
    slot = await first_slot(crm)

    first = await crm.create_booking(BookingRequest(slot, CLIENT, "key-1"))
    second = await crm.create_booking(BookingRequest(slot, CLIENT, "key-1"))

    assert first == second
    assert len(crm.bookings) == 1


async def test_slot_taken_by_someone_else(crm: FakeCRM) -> None:
    slot = await first_slot(crm)
    crm.block(slot)

    with pytest.raises(SlotTakenError):
        await crm.check_slot(slot)


async def test_slot_outside_schedule_is_taken(crm: FakeCRM) -> None:
    night = Slot("11", "102", datetime(2026, 10, 20, 23, 0, tzinfo=MSK), 90)

    with pytest.raises(SlotTakenError):
        await crm.check_slot(night)


async def test_master_without_service(crm: FakeCRM) -> None:
    with pytest.raises(NotFoundError):
        await crm.check_slot(Slot("21", "102", datetime(2026, 10, 20, 12, 0, tzinfo=MSK), 90))


async def test_failure_mode(crm: FakeCRM) -> None:
    crm.fail_next(2)

    for _ in range(2):
        with pytest.raises(ProviderUnavailableError):
            await crm.list_services()
    assert len(await crm.list_services()) == 12  # после двух сбоев CRM снова отвечает
    assert crm.calls == ["list_services"] * 3


async def test_busy_windows_are_deterministic() -> None:
    first = await FakeCRM(clock=lambda: NOW).find_slots("102", TUESDAY, TUESDAY)
    second = await FakeCRM(clock=lambda: NOW).find_slots("102", TUESDAY, TUESDAY)

    assert first == second
