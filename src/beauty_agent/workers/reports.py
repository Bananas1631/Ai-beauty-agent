"""Еженедельный отчёт салону в админ-бот и месячная выгрузка для счёта по тарифу «Результат».

Отчёт по понедельникам за прошлую неделю (по времени салона):
- записи через ИИ и их оценка в рублях — число записей × средний чек (из настроек салона,
  иначе — средний чек визитов салона за 90 дней);
- ответы клиентам вне рабочего времени администраторов — то, что без ИИ ждало бы утра;
- стоимость LLM на салон — ответы агента и черновики, которые не ушли клиенту.
Источник цифр — по готовности инфраструктуры: с ClickHouse (CLICKHOUSE_ENABLED) — аналитика по
событиям в ClickHouse (analytics/queries.py); с Kafka (KAFKA_ENABLED) — salon_week_stats, которую
копит потребитель weekly-stats (events/handlers/reports.py); без них — аналитика на чистом SQL
(db/queries/analytics, AnalyticsRepository). Средний чек — всегда из PostgreSQL: это деньги салона.
Выгрузка для счёта строится по правилу domain/billing.py — тому же, что в договоре.
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import Time, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.analytics.clickhouse import ClickHouse
from beauty_agent.analytics.queries import Analytics
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.db.models import Draft, Message, Salon, SalonWeekStats
from beauty_agent.db.repositories.analytics import AnalyticsRepository
from beauty_agent.domain.entities import format_rub
from beauty_agent.domain.enums import DraftStatus, MessageRole
from beauty_agent.reports.excel import collect_invoice, invoice_filename, invoice_workbook
from beauty_agent.reports.invoice import InvoiceLine, billable_bookings, invoice_csv
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
    analytics = AnalyticsRepository(session, salon.id)
    return await analytics.average_check(now - timedelta(days=AVERAGE_CHECK_DAYS), now, timezone=salon.timezone)


async def weekly_report(
    session: AsyncSession,
    salon: Salon,
    now: datetime,
    *,
    from_events: bool = False,
    analytics: Analytics | None = None,
) -> WeeklyReport:
    tz = ZoneInfo(salon.timezone)
    today = now.astimezone(tz).date()
    end = datetime.combine(today - timedelta(days=today.weekday()), time(), tzinfo=tz)  # понедельник 00:00
    start = end - timedelta(days=7)
    if analytics is not None:
        week = await analytics.week_stats(
            salon.id,
            start,
            end,
            timezone=salon.timezone,
            work_start=salon.config.work_start,
            work_end=salon.config.work_end,
        )
        return WeeklyReport(
            salon_name=salon.name,
            first_day=start,
            ai_bookings=week.ai_bookings,
            average_check_rub=await average_check(session, salon, now),
            answers=week.answers,
            off_hours_answers=week.off_hours_answers,
            llm_cost_rub=week.llm_cost_rub,
        )
    if from_events:
        stats = await session.scalar(
            select(SalonWeekStats).where(SalonWeekStats.salon_id == salon.id, SalonWeekStats.week_start == start.date())
        )
        return WeeklyReport(
            salon_name=salon.name,
            first_day=start,
            ai_bookings=stats.ai_bookings if stats else 0,
            average_check_rub=await average_check(session, salon, now),
            answers=stats.answers if stats else 0,
            off_hours_answers=stats.off_hours_answers if stats else 0,
            llm_cost_rub=stats.llm_cost_rub if stats else Decimal(0),
        )
    config = salon.config
    share = await AnalyticsRepository(session, salon.id).ai_share(
        start, end, timezone=salon.timezone, work_start=config.work_start, work_end=config.work_end
    )
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
        ai_bookings=share.ai_bookings,
        average_check_rub=await average_check(session, salon, now),
        answers=int(answers),
        off_hours_answers=int(off_hours_answers),
        llm_cost_rub=Decimal(message_cost or 0) + (draft_cost or Decimal(0)),
    )


async def send_weekly_reports(
    session_factory: async_sessionmaker[AsyncSession],
    notifier: Notifier,
    *,
    now: datetime | None = None,
    from_events: bool = False,
    analytics: Analytics | None = None,
) -> int:
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        salons = list(await session.scalars(select(Salon).where(Salon.is_demo.is_(False))))
        reports = [
            (salon.id, await weekly_report(session, salon, now, from_events=from_events, analytics=analytics))
            for salon in salons
        ]
    for salon_id, report in reports:
        await notifier.notify(AdminEvent(AdminEventKind.WEEKLY_REPORT, str(salon_id), None, report.text()))
    logger.info("weekly_reports_sent", salons=len(reports))
    return len(reports)


DocumentSender = Callable[[uuid.UUID, str, bytes, str], Awaitable[int]]


def previous_month(now: datetime) -> str:
    first = now.replace(day=1)
    last_month = first - timedelta(days=1)
    return f"{last_month:%Y-%m}"


async def send_monthly_invoices(
    session_factory: async_sessionmaker[AsyncSession],
    send: DocumentSender,
    *,
    now: datetime | None = None,
    analytics: Analytics | None = None,
) -> int:
    """1-го числа: выгрузка за прошлый месяц каждому салону (владельцам в админ-бот). Сколько салонов."""
    now = now or datetime.now(UTC)
    month = previous_month(now)
    sent = 0
    async with session_factory() as session:
        salons = list(await session.scalars(select(Salon).where(Salon.is_demo.is_(False))))
        for salon in salons:
            data = await collect_invoice(session, salon, month, now=now, analytics=analytics)
            caption = f"{salon.name}: выгрузка для счёта за {month} — записей через ИИ: {len(data.lines)}"
            if await send(salon.id, invoice_filename(salon, month), invoice_workbook(data), caption):
                sent += 1
    logger.info("monthly_invoices_sent", salons=sent, month=month)
    return sent


async def _monthly_invoices() -> int:
    current = runtime()
    admin = current.admin_bot_notifier
    if admin is None:
        logger.warning("monthly_invoices_skipped", reason="admin bot is not configured")
        return 0
    settings = current.settings
    clickhouse = await ClickHouse.connect(settings.clickhouse) if settings.clickhouse.enabled else None
    try:
        return await send_monthly_invoices(
            current.session_factory,
            lambda salon_id, name, content, caption: admin.send_document_to_owners(
                salon_id, name, content, caption=caption
            ),
            analytics=Analytics(clickhouse) if clickhouse is not None else None,
        )
    finally:
        if clickhouse is not None:
            await clickhouse.close()


@celery.task(name="periodic.monthly_invoice")
def monthly_invoice_task() -> int:
    sent: int = run(_monthly_invoices())
    return sent


async def _weekly_reports() -> int:
    current = runtime()
    settings = current.settings
    clickhouse = await ClickHouse.connect(settings.clickhouse) if settings.clickhouse.enabled else None
    try:
        return await send_weekly_reports(
            current.session_factory,
            current.notifier,
            from_events=settings.kafka.enabled,
            analytics=Analytics(clickhouse) if clickhouse is not None else None,
        )
    finally:
        if clickhouse is not None:
            await clickhouse.close()


@celery.task(name="periodic.weekly_report")
def weekly_report_task() -> int:
    sent: int = run(_weekly_reports())
    return sent


__all__ = ["InvoiceLine", "billable_bookings", "invoice_csv", "send_weekly_reports", "weekly_report"]
