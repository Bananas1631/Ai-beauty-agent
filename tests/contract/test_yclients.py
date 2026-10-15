"""Контрактные тесты адаптера YCLIENTS на сохранённых ответах: HTTP подменяется через respx.

Если YCLIENTS поменяет формат, упадёт разбор конкретного метода — сразу видно где.
"""

import json
from collections.abc import AsyncIterator, Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx
from pydantic import SecretStr
from tenacity import wait_none

from beauty_agent.adapters.crm.base import BookingProvider
from beauty_agent.adapters.crm.yclients import (
    YclientsBadResponseError,
    YclientsProvider,
    YclientsTimeoutError,
    numeric_id,
    parse_company,
    parse_dates,
    parse_services,
    parse_staff,
    parse_times,
)
from beauty_agent.core.config import YclientsSettings
from beauty_agent.core.resilience import CircuitBreaker, CircuitOpenError
from beauty_agent.domain.entities import BookingRequest, ClientContact, Slot
from beauty_agent.domain.errors import (
    ConfirmationRequiredError,
    DomainValidationError,
    NotFoundError,
    ProviderAuthError,
    ProviderUnavailableError,
    SlotTakenError,
)

FIXTURES = Path(__file__).parent / "fixtures" / "yclients"
BASE = "https://api.yclients.test/api/v1/"
COMPANY = 555001
MSK = ZoneInfo("Europe/Moscow")
SLOT = Slot("11", "101", datetime(2026, 10, 20, 10, 0, tzinfo=MSK), 90)
CLIENT = ClientContact(name="Анна", phone="+79000000001")


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# Один SSL-контекст на модуль: создавать его на каждый клиент — полсекунды на тест в Windows
SSL_CONTEXT = httpx.create_ssl_context()


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=BASE, verify=SSL_CONTEXT)


class NoLimit:
    async def acquire(self) -> None:
        return None


def make_settings(*, user_token: str | None = None) -> YclientsSettings:
    return YclientsSettings(
        base_url=BASE,
        partner_token=SecretStr("partner-test"),
        user_token=SecretStr(user_token) if user_token else None,
    )


@pytest.fixture
async def provider() -> AsyncIterator[YclientsProvider]:
    yclients = YclientsProvider(COMPANY, make_settings(), http=http_client(), limiter=NoLimit(), retry_wait=wait_none())
    try:
        yield yclients
    finally:
        await yclients.aclose()


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------- разбор ответов


def test_parse_company() -> None:
    info = parse_company(fixture("company.json")["data"])

    assert info.external_id == "555001"
    assert info.name == "Лаванда — студия красоты"
    assert info.timezone == "Europe/Moscow"
    assert info.site_url == "https://lavanda.example"


def test_parse_services_skips_archived_and_converts_units() -> None:
    services = parse_services(fixture("book_services.json")["data"])

    assert [service.external_id for service in services] == ["101", "102", "103", "201", "202"]
    manicure = services[0]
    assert manicure.duration_min == 90  # 5400 секунд
    assert manicure.category == "Ногтевой сервис"
    assert manicure.price_text() == "от 2 500 до 3 000 ₽"
    assert services[1].price_text() == "3 200 ₽"
    assert services[2].description is None  # null в ответе


def test_parse_staff() -> None:
    masters = parse_staff(fixture("book_staff.json")["data"], service_id="101", bookable_only=True)

    assert [master.name for master in masters] == ["Ольга", "Марина"]  # Светлана не принимает онлайн
    assert masters[1].specialization == "Мастер маникюра"  # из должности, когда специализация пустая
    assert masters[0].service_ids == frozenset({"101"})


def test_parse_dates_filters_range() -> None:
    days = parse_dates(fixture("book_dates.json")["data"], date(2026, 10, 21), date(2026, 10, 25))

    assert days == [date(2026, 10, 21), date(2026, 10, 23)]


def test_parse_times() -> None:
    slots = parse_times(
        fixture("book_times.json")["data"], master_id="11", service_id="101", day=date(2026, 10, 20), tz=MSK
    )

    assert [slot.starts_at.strftime("%H:%M") for slot in slots] == ["10:00", "13:30", "18:15"]
    assert slots[0].starts_at.utcoffset() is not None
    assert slots[0].duration_min == 90


def test_parse_times_without_datetime_uses_salon_timezone() -> None:
    slots = parse_times(
        [{"time": "11:45", "seance_length": 3600}], master_id="11", service_id="101", day=date(2026, 10, 20), tz=MSK
    )

    assert slots[0].starts_at == datetime(2026, 10, 20, 11, 45, tzinfo=MSK)


@pytest.mark.parametrize("value", ["../company/1", "12a", "١٢", "", "9" * 19])
def test_numeric_id_rejects_path_injection(value: str) -> None:
    with pytest.raises(DomainValidationError):
        numeric_id(value, "мастер")


# ---------------------------------------------------------------- HTTP


def test_provider_matches_the_protocol(provider: YclientsProvider) -> None:
    checked: BookingProvider = provider
    assert checked is provider


def test_requires_partner_token() -> None:
    with pytest.raises(ProviderAuthError):
        YclientsProvider(COMPANY, YclientsSettings(base_url=BASE))


async def test_auth_headers(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.get(f"company/{COMPANY}").respond(200, json=fixture("company.json"))

    await provider.get_salon_info()

    request = route.calls.last.request
    assert request.headers["Authorization"] == "Bearer partner-test"
    assert request.headers["Accept"] == "application/vnd.yclients.v2+json"


async def test_user_token_is_added() -> None:
    settings = make_settings(user_token="user-test")
    yclients = YclientsProvider(COMPANY, settings, http=http_client(), limiter=NoLimit())
    assert yclients.headers()["Authorization"] == "Bearer partner-test, User user-test"
    await yclients.aclose()


async def test_find_slots_walks_dates_and_masters(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.get(f"book_staff/{COMPANY}").respond(200, json=fixture("book_staff.json"))
    dates = api.get(f"book_dates/{COMPANY}").respond(200, json=fixture("book_dates.json"))
    times = api.get(url__regex=rf"book_times/{COMPANY}/\d+/2026-10-2\d").respond(200, json=fixture("book_times.json"))

    slots = await provider.find_slots("101", date(2026, 10, 20), date(2026, 10, 27))

    assert dates.calls.last.request.url.params["service_ids[]"] == "101"
    assert times.call_count == 6  # 3 дня с окнами × 2 мастера, принимающих онлайн-запись
    assert slots == sorted(slots, key=lambda slot: (slot.starts_at, slot.master_id))
    assert {slot.master_id for slot in slots} == {"11", "12"}


async def test_find_slots_without_masters(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.get(f"book_staff/{COMPANY}").respond(200, json={"success": True, "data": [], "meta": []})

    assert await provider.find_slots("101", date(2026, 10, 20), date(2026, 10, 27)) == []


async def test_create_booking(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.post(f"book_record/{COMPANY}").respond(201, json=fixture("book_record.json"))

    result = await provider.create_booking(BookingRequest(SLOT, CLIENT, "dialog-1-option-1"))

    assert result.external_id == "900001"
    assert result.record_hash == "a1b2c3d4e5"
    body = json.loads(route.calls.last.request.content)
    assert body["phone"] == "79000000001"
    assert body["api_id"] == "dialog-1-option-1"
    assert body["appointments"] == [
        {"id": 1, "services": [101], "staff_id": 11, "datetime": "2026-10-20T10:00:00+03:00"}
    ]


async def test_slot_taken(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.post(f"book_check/{COMPANY}").respond(422, json=fixture("book_check_busy.json"))

    with pytest.raises(SlotTakenError):
        await provider.check_slot(SLOT)


async def test_slot_free(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.post(f"book_check/{COMPANY}").respond(200, json=fixture("book_check_ok.json"))

    await provider.check_slot(SLOT)


async def test_confirmation_code_required(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.post(f"book_record/{COMPANY}").respond(422, json=fixture("book_code_required.json"))

    with pytest.raises(ConfirmationRequiredError):
        await provider.create_booking(BookingRequest(SLOT, CLIENT, "key"))


async def test_unauthorized_is_not_retried(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.get(f"book_services/{COMPANY}").respond(401, json=fixture("error_401.json"))

    with pytest.raises(ProviderAuthError):
        await provider.list_services()
    assert route.call_count == 1


async def test_not_found(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.get(f"company/{COMPANY}").respond(404, json={"success": False, "data": None, "meta": {"message": "нет"}})

    with pytest.raises(NotFoundError):
        await provider.get_salon_info()


async def test_429_is_retried_three_times(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.get(f"book_services/{COMPANY}").respond(429, json=fixture("error_429.json"))

    with pytest.raises(ProviderUnavailableError):
        await provider.list_services()
    assert route.call_count == 3


async def test_500_then_success(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.get(f"book_services/{COMPANY}").mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json=fixture("book_services.json"))]
    )

    assert len(await provider.list_services()) == 5


async def test_booking_is_never_retried(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.post(f"book_record/{COMPANY}").respond(500)

    with pytest.raises(ProviderUnavailableError):
        await provider.create_booking(BookingRequest(SLOT, CLIENT, "key"))
    assert route.call_count == 1


async def test_timeout_on_booking_without_user_token(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.post(f"book_record/{COMPANY}").mock(side_effect=httpx.ReadTimeout("slow"))

    with pytest.raises(YclientsTimeoutError):
        await provider.create_booking(BookingRequest(SLOT, CLIENT, "key"))


async def test_timeout_on_booking_finds_created_record(api: respx.MockRouter) -> None:
    yclients = YclientsProvider(
        COMPANY, make_settings(user_token="user-test"), http=http_client(), limiter=NoLimit(), retry_wait=wait_none()
    )
    api.post(f"book_record/{COMPANY}").mock(side_effect=httpx.ReadTimeout("slow"))
    records = api.get(f"records/{COMPANY}").respond(200, json=fixture("records.json"))

    result = await yclients.create_booking(BookingRequest(SLOT, CLIENT, "dialog-1-option-1"))

    assert result.external_id == "900001"  # запись дошла до YCLIENTS — второй не создаём
    assert records.calls.last.request.url.params["staff_id"] == "11"
    await yclients.aclose()


async def test_unexpected_payload(provider: YclientsProvider, api: respx.MockRouter) -> None:
    route = api.get(f"company/{COMPANY}").respond(200, json={"success": True, "data": {"title": "без id"}})

    with pytest.raises(YclientsBadResponseError):
        await provider.get_salon_info()
    assert route.call_count == 1  # формат не исправится от повтора


async def test_html_instead_of_json(provider: YclientsProvider, api: respx.MockRouter) -> None:
    api.get(f"company/{COMPANY}").respond(200, text="<html>maintenance</html>")

    with pytest.raises(YclientsBadResponseError):
        await provider.get_salon_info()


async def test_circuit_opens_after_failures(api: respx.MockRouter) -> None:
    breaker = CircuitBreaker("test", failure_threshold=5, recovery_s=30, clock=lambda: 0.0)
    yclients = YclientsProvider(
        COMPANY, make_settings(), http=http_client(), limiter=NoLimit(), breaker=breaker, retry_wait=wait_none()
    )
    route = api.get(f"book_services/{COMPANY}").respond(503)

    for _ in range(2):  # 2 вызова × 3 попытки = 6 запросов, цепь размыкается на пятом
        with pytest.raises(ProviderUnavailableError):
            await yclients.list_services()
    assert route.call_count == 5

    with pytest.raises(CircuitOpenError):
        await yclients.list_services()
    assert route.call_count == 5  # разомкнутая цепь не отправляет запросы
    await yclients.aclose()
