"""Еженедельный отчёт салону в админ-бот и месячная выгрузка для счёта по тарифу «Результат».

Отчёт по понедельникам за прошлую неделю (по времени салона):
- записи через ИИ и их оценка в рублях — число записей × средний чек (из настроек салона,
  иначе — средняя цена записей салона за 90 дней);
- ответы клиентам вне рабочего времени администраторов — то, что без ИИ ждало бы утра;
- стоимость LLM на салон — ответы агента и черновики, которые не ушли клиенту.
Выгрузка для счёта строится по правилу domain/billing.py — тому же, что в договоре.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Time, and_, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.db.models import Booking, Draft, Master, Message, Salon, Service
from beauty_agent.domain.billing import BILLABLE_STATUSES, BillingPeriod, invoice_total
from beauty_agent.domain.entities import format_rub
from beauty_agent.domain.enums import BookingStatus, DraftStatus, MessageRole
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

AVERAGE_CHECK_DAYS = 90


@dataclass(frozen=True, slots=True)
class WeeklyReport:
    salon_name: str
    first_day: datetime  # начало недели по времени салона
    ai_bookings: int
    average_check_rub: Decimal | None
    answers: int
    off_hours_answers: int
    llm_cost_rub: Decimal

    @property
    def ai_revenue_rub(self) -> Decimal | None:
        return None if self.average_check_rub is None else self.average_check_rub * self.ai_bookings

    def text(self) -> str:
        last_day = self.first_day + timedelta(days=6)
        lines = [
            f"{self.salon_name}, неделя {self.first_day:%d.%m}–{last_day:%d.%m}",
            f"Записей через ИИ: {self.ai_bookings}",
        ]
        revenue = self.ai_revenue_rub
        if revenue is not None and self.average_check_rub is not None:
            lines.append(
                f"Это примерно {format_rub(revenue.quantize(Decimal(1)))} ₽ "
                f"(средний чек {format_rub(self.average_check_rub.quantize(Decimal(1)))} ₽)"
            )
        lines += [
            f"Ответов клиентам: {self.answers}, из них вне рабочего времени: {self.off_hours_answers}",
            f"Расход на ИИ: {format_rub(self.llm_cost_rub.quantize(Decimal('0.01')))} ₽",
        ]
        return "\n".join(lines)


async def average_check(session: AsyncSession, salon: Salon, now: datetime) -> Decimal | None:
    configured = salon.config.average_check_rub
    if configured is not None:
        return configured
    value = await session.scalar(
        select(func.avg(Booking.price)).where(
            Booking.salon_id == salon.id,
            Booking.price.is_not(None),
            Booking.status != BookingStatus.CANCELLED,
            Booking.starts_at >= now - timedelta(days=AVERAGE_CHECK_DAYS),
        )
    )
    return Decimal(value) if value is not None else None


async def weekly_report(session: AsyncSession, salon: Salon, now: datetime) -> WeeklyReport:
    tz = ZoneInfo(salon.timezone)
    today = now.astimezone(tz).date()
    end = datetime.combine(today - timedelta(days=today.weekday()), time(), tzinfo=tz)  # понедельник 00:00
    start = end - timedelta(days=7)
    ai_bookings = await session.scalar(
        select(func.count()).where(
            Booking.salon_id == salon.id,
            Booking.created_by_ai,
            Booking.status != BookingStatus.CANCELLED,
            Booking.created_at >= start,
            Booking.created_at < end,
        )
    )
    config = salon.config
    local_time = cast(func.timezone(salon.timezone, Message.created_at), Time)
    off_hours = or_(local_time < config.work_start, local_time >= config.work_end)
    answers, off_hours_answers, message_cost = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(off_hours),
                func.coalesce(func.sum(Message.cost_rub), 0),
            ).where(
                Message.salon_id == salon.id,
                Message.role == MessageRole.AGENT,
                Message.created_at >= start,
                Message.created_at < end,
            )
        )
    ).one()
    # стоимость черновиков, которые не стали сообщениями (у отправленных она перенесена в сообщение)
    draft_cost = await session.scalar(
        select(func.coalesce(func.sum(Draft.cost_rub), 0)).where(
            Draft.salon_id == salon.id,
            Draft.status.in_([DraftStatus.PENDING, DraftStatus.MANUAL]),
            Draft.created_at >= start,
            Draft.created_at < end,
        )
    )
    return WeeklyReport(
        salon_name=salon.name,
        first_day=start,
        ai_bookings=int(ai_bookings or 0),
        average_check_rub=await average_check(session, salon, now),
        answers=int(answers),
        off_hours_answers=int(off_hours_answers),
        llm_cost_rub=Decimal(message_cost or 0) + (draft_cost or Decimal(0)),
    )


async def send_weekly_reports(
    session_factory: async_sessionmaker[AsyncSession], notifier: Notifier, *, now: datetime | None = None
) -> int:
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        salons = list(await session.scalars(select(Salon).where(Salon.is_demo.is_(False))))
        reports = [(salon.id, await weekly_report(session, salon, now)) for salon in salons]
    for salon_id, report in reports:
        await notifier.notify(AdminEvent(AdminEventKind.WEEKLY_REPORT, str(salon_id), None, report.text()))
    logger.info("weekly_reports_sent", salons=len(reports))
    return len(reports)


# ---------------------------------------------------------------- выгрузка для счёта


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


@celery.task(name="periodic.weekly_report")
def weekly_report_task() -> int:
    current = runtime()
    sent: int = run(send_weekly_reports(current.session_factory, current.notifier))
    return sent
