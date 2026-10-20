"""Оплачиваемые записи месяца по тарифу «Результат» — для CSV, Excel и API.

Выборка повторяет правило domain/billing.is_billable (тест сверяет их). Модуль без Celery:
его импортируют и воркер, и API (выгрузка из админки), и скрипты.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Booking, Draft, Master, Message, Service
from beauty_agent.domain.billing import BILLABLE_STATUSES, BillingPeriod, invoice_total
from beauty_agent.domain.enums import BookingStatus, DraftStatus, MessageRole


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    starts_at: datetime
    service: str
    master: str
    status: BookingStatus
    external_id: str | None


async def billable_bookings(
    session: AsyncSession, salon_id: uuid.UUID, period: BillingPeriod, now: datetime
) -> list[InvoiceLine]:
    """Те же условия, что domain/billing.is_billable — тест проверяет, что они совпадают."""
    statement = (
        select(Booking, Service.name, Master.name)
        .join(Service, Service.id == Booking.service_id)
        .join(Master, Master.id == Booking.master_id)
        .where(
            and_(
                Booking.salon_id == salon_id,
                Booking.created_by_ai,
                Booking.status.in_(list(BILLABLE_STATUSES)),
                Booking.starts_at >= period.start,
                Booking.starts_at < period.end,
                Booking.starts_at <= now,
            )
        )
        .order_by(Booking.starts_at)
    )
    return [
        InvoiceLine(booking.starts_at, service, master, booking.status, booking.external_id)
        for booking, service, master in await session.execute(statement)
    ]


def invoice_csv(lines: list[InvoiceLine], timezone: str) -> str:
    """CSV для счёта: одна строка на оплачиваемую запись и итог."""
    tz = ZoneInfo(timezone)
    rows = ["Дата визита;Услуга;Мастер;Статус;ID записи в CRM;Сумма, ₽"]
    for line in lines:
        cells = [
            line.starts_at.astimezone(tz).strftime("%d.%m.%Y %H:%M"),
            line.service,
            line.master,
            line.status.value,
            line.external_id or "",
            "59",
        ]
        rows.append(";".join(cell.replace(";", ",").replace("\n", " ") for cell in cells))
    rows.append(f"Итого;;;;{len(lines)} записей;{invoice_total(len(lines))}")
    return "\n".join(rows) + "\n"


async def llm_cost(session: AsyncSession, salon_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
    """Расход на модели за период по PostgreSQL: ответы агента и черновики, не ставшие сообщениями.

    С ClickHouse та же цифра берётся из событий llm.call.finished (analytics/queries.py).
    """
    messages = await session.scalar(
        select(func.coalesce(func.sum(Message.cost_rub), 0)).where(
            Message.salon_id == salon_id,
            Message.role == MessageRole.AGENT,
            Message.created_at >= start,
            Message.created_at < end,
        )
    )
    drafts = await session.scalar(
        select(func.coalesce(func.sum(Draft.cost_rub), 0)).where(
            Draft.salon_id == salon_id,
            Draft.status.in_([DraftStatus.PENDING, DraftStatus.MANUAL]),
            Draft.created_at >= start,
            Draft.created_at < end,
        )
    )
    return Decimal(messages or 0) + Decimal(drafts or 0)
