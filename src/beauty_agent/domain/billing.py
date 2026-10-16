"""Правило атрибуции для тарифа «Результат»: какая запись считается сделанной ИИ и попадает в счёт.

Это правило одно и в коде, и в договоре с салоном (приложение «Порядок расчёта»):

    Запись оплачивается (59 ₽), если одновременно:
    1. её создал ИИ-администратор (created_by_ai), а не администратор салона вручную;
    2. время визита попадает в расчётный месяц (по часовому поясу салона);
    3. визит уже наступил и запись не была отменена до него — статус «подтверждена»,
       «визит состоялся» или «клиент не пришёл». Неявка оплачивается: запись была настоящей,
       её не отменили, слот был занят. Запись «ждёт подтверждения» не оплачивается, пока её
       не подтвердил администратор.

Счёт строится по месяцу визита, а не создания записи: отмену видно только до визита.
SQL-выборка для счёта (workers/reports.py) повторяет эти условия; тест сверяет её с is_billable.
"""

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from beauty_agent.domain.enums import BookingStatus

PRICE_PER_BOOKING_RUB = Decimal(59)
BILLABLE_STATUSES = frozenset({BookingStatus.CONFIRMED, BookingStatus.COMPLETED, BookingStatus.NO_SHOW})


@dataclass(frozen=True, slots=True)
class BillingPeriod:
    """Расчётный месяц салона: [start, end) в UTC-моментах, границы — полночь по времени салона."""

    start: datetime
    end: datetime

    @classmethod
    def month(cls, year: int, month: int, timezone: str) -> "BillingPeriod":
        tz = ZoneInfo(timezone)
        first = date(year, month, 1)
        following = date(year + month // 12, month % 12 + 1, 1)
        return cls(datetime.combine(first, time(), tzinfo=tz), datetime.combine(following, time(), tzinfo=tz))


@dataclass(frozen=True, slots=True)
class BookingFacts:
    created_by_ai: bool
    status: BookingStatus
    starts_at: datetime


def is_billable(booking: BookingFacts, period: BillingPeriod, now: datetime) -> bool:
    return (
        booking.created_by_ai
        and booking.status in BILLABLE_STATUSES
        and period.start <= booking.starts_at < period.end
        and booking.starts_at <= now
    )


def invoice_total(billable_count: int) -> Decimal:
    return PRICE_PER_BOOKING_RUB * billable_count
