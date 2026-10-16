"""Доменные объекты: неизменяемые dataclass без БД и HTTP.

Ими обмениваются движок записи, агент и адаптеры CRM. ID из CRM — строки:
у YCLIENTS это числа, у другой CRM могут быть UUID, а домену всё равно.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal


def format_rub(value: Decimal) -> str:
    """2500 → «2 500», 1499.50 → «1 499,50»."""
    if value == value.to_integral_value():
        return f"{int(value):,}".replace(",", " ")
    return f"{value:,.2f}".replace(",", " ").replace(".", ",")


@dataclass(frozen=True, slots=True)
class SalonInfo:
    """Карточка филиала из CRM: из неё создаётся или обновляется Salon."""

    external_id: str
    name: str
    timezone: str
    address: str | None = None
    phone: str | None = None
    site_url: str | None = None
    working_hours: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class ServiceInfo:
    external_id: str
    name: str
    duration_min: int
    price_min: Decimal | None = None
    price_max: Decimal | None = None
    category: str | None = None
    description: str | None = None

    def price_text(self) -> str:
        """Цена словами — ровно из каталога: никаких цифр «посередине диапазона»."""
        low, high = self.price_min, self.price_max
        if low is not None and high is not None and low != high:
            return f"от {format_rub(low)} до {format_rub(high)} ₽"
        single = low if low is not None else high
        if single is None:
            return "цену уточнит мастер"
        return f"{format_rub(single)} ₽"


@dataclass(frozen=True, slots=True)
class MasterInfo:
    """Мастер — «сотрудник» YCLIENTS."""

    external_id: str
    name: str
    specialization: str | None = None
    service_ids: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True, slots=True)
class Slot:
    """Свободное окно. ID слота у YCLIENTS нет: его задаёт тройка «мастер + услуга + время начала»."""

    master_id: str
    service_id: str
    starts_at: datetime
    duration_min: int

    def __post_init__(self) -> None:
        if self.starts_at.tzinfo is None:
            raise ValueError("Slot.starts_at: нужно время с часовым поясом")
        if self.duration_min <= 0:
            raise ValueError("Slot.duration_min должна быть положительной")

    @property
    def ends_at(self) -> datetime:
        return self.starts_at + timedelta(minutes=self.duration_min)

    @property
    def key(self) -> str:
        return f"{self.master_id}|{self.service_id}|{self.starts_at.isoformat()}"


@dataclass(frozen=True, slots=True)
class ClientContact:
    name: str
    phone: str  # E.164: +79161234567
    email: str | None = None


@dataclass(frozen=True, slots=True)
class BookingRequest:
    slot: Slot
    client: ClientContact
    idempotency_key: str
    comment: str | None = None

    def crm_comment(self) -> str:
        parts = [self.comment] if self.comment else []
        parts.append("Запись через ИИ-администратора")
        return ". ".join(parts)


@dataclass(frozen=True, slots=True)
class BookingResult:
    external_id: str
    slot: Slot
    record_hash: str | None = None


@dataclass(frozen=True, slots=True)
class CrmBooking:
    """Запись в CRM по её ID: администратор мог перенести или отменить её прямо в YCLIENTS."""

    external_id: str
    slot: Slot
    active: bool = True  # False — запись удалена или отменена в CRM
