"""Фейковый YCLIENTS в памяти: салон с 4 мастерами, 12 услугами, графиком и занятыми окнами.

Нужен для тестов, чата в терминале, демо без доступа к CRM и evals. Ведёт себя как настоящий
провайдер: свободное время считает модуль slots, повтор записи с тем же ключом не создаёт дубль,
занятое время даёт SlotTakenError. Текущее время передаётся в конструктор — тесты детерминированы.
Режим сбоев (fail_next) имитирует недоступную CRM для тестов деградации.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from beauty_agent.domain.booking_engine.slots import Interval, free_starts
from beauty_agent.domain.entities import (
    BookingRequest,
    BookingResult,
    CrmBooking,
    MasterInfo,
    SalonInfo,
    ServiceInfo,
    Slot,
)
from beauty_agent.domain.errors import DomainError, NotFoundError, ProviderUnavailableError, SlotTakenError

Clock = Callable[[], datetime]

FAKE_SALON = SalonInfo(
    external_id="fake-1",
    name="Студия красоты «Лаванда»",
    timezone="Europe/Moscow",
    address="г. Москва, ул. Примерная, д. 1, 2 этаж",
    phone="+7 900 000-00-00",
    site_url=None,
    working_hours="Ежедневно с 10:00 до 20:00",
    description="Маникюр, педикюр, стрижки и окрашивание, брови и ресницы",
)


def _service(external_id: str, name: str, minutes: int, low: int, high: int, category: str) -> ServiceInfo:
    return ServiceInfo(external_id, name, minutes, Decimal(low), Decimal(high), category)


FAKE_SERVICES: tuple[ServiceInfo, ...] = (
    _service("101", "Маникюр без покрытия", 60, 1500, 1500, "Ногтевой сервис"),
    _service("102", "Маникюр с покрытием гель-лак", 90, 2500, 3000, "Ногтевой сервис"),
    _service("103", "Снятие гель-лака", 30, 500, 500, "Ногтевой сервис"),
    _service("104", "Педикюр с покрытием", 120, 3200, 3200, "Ногтевой сервис"),
    _service("105", "Наращивание ногтей", 150, 4000, 4500, "Ногтевой сервис"),
    _service("201", "Женская стрижка", 60, 2000, 3500, "Волосы"),
    _service("202", "Мужская стрижка", 45, 1500, 1500, "Волосы"),
    _service("203", "Окрашивание в один тон", 150, 5000, 8000, "Волосы"),
    _service("204", "Укладка", 45, 1800, 1800, "Волосы"),
    _service("301", "Коррекция и окрашивание бровей", 45, 1500, 1800, "Брови и ресницы"),
    _service("302", "Ламинирование ресниц", 60, 2500, 2500, "Брови и ресницы"),
    _service("303", "Наращивание ресниц 2D", 150, 3500, 4000, "Брови и ресницы"),
)


@dataclass(frozen=True, slots=True)
class _Schedule:
    weekdays: frozenset[int]  # 0 — понедельник
    start: time
    end: time
    break_start: time
    break_minutes: int


FAKE_MASTERS: tuple[tuple[MasterInfo, _Schedule], ...] = (
    (
        MasterInfo("11", "Ольга", "Топ-мастер ногтевого сервиса", frozenset({"101", "102", "103", "104", "105"})),
        _Schedule(frozenset({0, 1, 2, 3, 4}), time(10), time(20), time(14), 30),
    ),
    (
        MasterInfo("12", "Марина", "Мастер маникюра", frozenset({"101", "102", "103"})),
        _Schedule(frozenset({2, 3, 4, 5, 6}), time(10), time(20), time(15), 30),
    ),
    (
        MasterInfo("21", "Алина", "Парикмахер-стилист", frozenset({"201", "202", "203", "204"})),
        _Schedule(frozenset({1, 2, 3, 4, 5}), time(10), time(20), time(14), 60),
    ),
    (
        MasterInfo("31", "Светлана", "Бровист, лэшмейкер", frozenset({"301", "302", "303"})),
        _Schedule(frozenset({0, 2, 4, 5, 6}), time(11), time(20), time(15), 30),
    ),
)

MAX_SEARCH_DAYS = 31


class FakeCRM:
    def __init__(
        self,
        *,
        clock: Clock | None = None,
        lead_time: timedelta = timedelta(hours=1),
        busy_per_day: int = 2,
    ) -> None:
        self.tz = ZoneInfo(FAKE_SALON.timezone)
        self.clock: Clock = clock or (lambda: datetime.now(UTC))
        self.lead_time = lead_time
        self.busy_per_day = busy_per_day
        self.bookings: dict[str, tuple[BookingRequest, BookingResult]] = {}
        # изменения «администратором» или через API: перенос и отмена по ID записи
        self.moved: dict[str, Slot] = {}
        self.cancelled: set[str] = set()
        self.blocked: list[tuple[str, Interval]] = []  # время, занятое «другими клиентами» в тестах
        self.calls: list[str] = []
        self._failures: list[DomainError] = []
        self._services = {service.external_id: service for service in FAKE_SERVICES}
        self._masters = {master.external_id: (master, schedule) for master, schedule in FAKE_MASTERS}

    # ------------------------------------------------------------- управление из тестов

    def fail_next(self, times: int = 1, error: DomainError | None = None) -> None:
        """Следующие times вызовов упадут с error (по умолчанию — CRM недоступна)."""
        self._failures.extend([error or ProviderUnavailableError()] * times)

    def block(self, slot: Slot) -> None:
        """Кто-то другой занял это время — проверка и запись дадут SlotTakenError."""
        self.blocked.append((slot.master_id, Interval(slot.starts_at, slot.ends_at)))

    def _enter(self, method: str) -> None:
        self.calls.append(method)
        if self._failures:
            raise self._failures.pop(0)

    # ------------------------------------------------------------- BookingProvider

    async def get_salon_info(self) -> SalonInfo:
        self._enter("get_salon_info")
        return FAKE_SALON

    async def list_services(self) -> list[ServiceInfo]:
        self._enter("list_services")
        return list(self._services.values())

    async def list_masters(self, service_id: str | None = None) -> list[MasterInfo]:
        self._enter("list_masters")
        return [
            master for master, _ in self._masters.values() if service_id is None or service_id in master.service_ids
        ]

    async def find_slots(
        self, service_id: str, date_from: date, date_to: date, master_id: str | None = None
    ) -> list[Slot]:
        self._enter("find_slots")
        service = self._service(service_id)
        last = min(date_to, date_from + timedelta(days=MAX_SEARCH_DAYS - 1))
        slots: list[Slot] = []
        for master, schedule in self._masters.values():
            if service_id not in master.service_ids or master_id not in (None, master.external_id):
                continue
            day = date_from
            while day <= last:
                slots.extend(self._day_slots(master.external_id, schedule, service, day))
                day += timedelta(days=1)
        return sorted(slots, key=lambda slot: (slot.starts_at, slot.master_id))

    async def check_slot(self, slot: Slot) -> None:
        self._enter("check_slot")
        self._ensure_free(slot)

    async def create_booking(self, request: BookingRequest) -> BookingResult:
        self._enter("create_booking")
        existing = self.bookings.get(request.idempotency_key)
        if existing is not None:
            return existing[1]
        self._ensure_free(request.slot)
        result = BookingResult(external_id=str(900_000 + len(self.bookings) + 1), slot=request.slot)
        self.bookings[request.idempotency_key] = (request, result)
        return result

    async def find_booking(self, slot: Slot, phone: str) -> BookingResult | None:
        self._enter("find_booking")
        for request, result in self.bookings.values():
            if self._current(result) == slot and request.client.phone == phone:
                return result
        return None

    async def get_booking(self, external_id: str) -> CrmBooking | None:
        self._enter("get_booking")
        result = self._result(external_id)
        if result is None:
            return None
        current = self._current(result)
        return CrmBooking(external_id, current or result.slot, active=current is not None)

    async def reschedule_booking(self, external_id: str, slot: Slot) -> BookingResult:
        self._enter("reschedule_booking")
        result = self._result(external_id)
        if result is None or external_id in self.cancelled:
            raise NotFoundError("Запись не найдена")
        if self._current(result) != slot:
            self._ensure_free(slot, ignore=external_id)
        self.moved[external_id] = slot
        return BookingResult(external_id, slot)

    async def cancel_booking(self, external_id: str) -> None:
        self._enter("cancel_booking")
        if self._result(external_id) is not None:
            self.cancelled.add(external_id)

    # ------------------------------------------------------------- записи

    def _result(self, external_id: str) -> BookingResult | None:
        for _, result in self.bookings.values():
            if result.external_id == external_id:
                return result
        return None

    def _current(self, result: BookingResult) -> Slot | None:
        """Текущее окно записи с учётом переноса; None — запись отменена."""
        if result.external_id in self.cancelled:
            return None
        return self.moved.get(result.external_id, result.slot)

    # ------------------------------------------------------------- расчёт

    def _service(self, service_id: str) -> ServiceInfo:
        service = self._services.get(service_id)
        if service is None:
            raise NotFoundError("Услуга не найдена")
        return service

    def _local(self, day: date, moment: time) -> datetime:
        return datetime.combine(day, moment, tzinfo=self.tz)

    def _busy(self, master_id: str, schedule: _Schedule, day: date, ignore: str | None = None) -> list[Interval]:
        """Перерыв, «чужие» записи (детерминированные по дате) и записи, созданные через фейк."""
        break_start = self._local(day, schedule.break_start)
        busy = [Interval(break_start, break_start + timedelta(minutes=schedule.break_minutes))]
        digest = hashlib.sha256(f"{master_id}:{day.isoformat()}".encode()).digest()
        work_hours = schedule.end.hour - schedule.start.hour
        for index in range(self.busy_per_day):
            hour = schedule.start.hour + digest[index] % max(work_hours - 1, 1)
            start = self._local(day, time(hour, 30 * (digest[index + 8] % 2)))
            busy.append(Interval(start, start + timedelta(minutes=60 + 30 * (digest[index + 16] % 3))))
        for _, result in self.bookings.values():
            current = self._current(result)
            if current is not None and current.master_id == master_id and result.external_id != ignore:
                busy.append(Interval(current.starts_at, current.ends_at))
        busy.extend(interval for blocked_master, interval in self.blocked if blocked_master == master_id)
        return busy

    def _day_slots(
        self, master_id: str, schedule: _Schedule, service: ServiceInfo, day: date, ignore: str | None = None
    ) -> list[Slot]:
        if day.weekday() not in schedule.weekdays:
            return []
        shift = Interval(self._local(day, schedule.start), self._local(day, schedule.end))
        starts = free_starts(
            [shift],
            self._busy(master_id, schedule, day, ignore),
            timedelta(minutes=service.duration_min),
            not_before=self.clock() + self.lead_time,
        )
        return [Slot(master_id, service.external_id, start, service.duration_min) for start in starts]

    def _ensure_free(self, slot: Slot, ignore: str | None = None) -> None:
        service = self._service(slot.service_id)
        entry = self._masters.get(slot.master_id)
        if entry is None or slot.service_id not in entry[0].service_ids:
            raise NotFoundError("Мастер не делает эту услугу")
        day = slot.starts_at.astimezone(self.tz).date()
        free = self._day_slots(slot.master_id, entry[1], service, day, ignore)
        if all(candidate.starts_at != slot.starts_at for candidate in free):
            raise SlotTakenError
