"""booking.attributed — запись засчитана ИИ, когда наступил визит (правило domain/billing.py).

Раз в 15 минут задача смотрит записи ИИ с визитом за последние трое суток и публикует событие
для тех, что подходят под правило тарифа «Результат». Событие одно на запись: dedupe_key
в event_outbox не даст записать его второй раз ни этой задаче, ни её повтору.

Окно трое суток — меньше срока хранения опубликованных событий в outbox (7 дней), поэтому
очистка outbox не приведёт к повторной атрибуции. Счёт по-прежнему строится SQL-выборкой
(workers/reports.py) по тому же правилу; событие — для потребителей: статистики и аналитики.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.db.models import Booking, Salon
from beauty_agent.domain.billing import BILLABLE_STATUSES, BillingPeriod, BookingFacts, is_billable
from beauty_agent.domain.events import BookingAttributed, BookingAttributedData
from beauty_agent.events.outbox import record_event
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

ATTRIBUTION_WINDOW = timedelta(days=3)


async def attribute_visits(session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None) -> int:
    """Сколько записей засчитано в этот раз."""
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(Booking, Salon.timezone)
                .join(Salon, Salon.id == Booking.salon_id)
                .where(
                    Booking.created_by_ai.is_(True),
                    Booking.status.in_(BILLABLE_STATUSES),
                    Booking.starts_at <= now,
                    Booking.starts_at > now - ATTRIBUTION_WINDOW,
                )
            )
        ).all()
        attributed = 0
        for booking, timezone in rows:
            local = booking.starts_at.astimezone(ZoneInfo(timezone))
            period = BillingPeriod.month(local.year, local.month, timezone)
            if not is_billable(BookingFacts(booking.created_by_ai, booking.status, booking.starts_at), period, now):
                continue
            event = BookingAttributed(
                salon_id=booking.salon_id,
                occurred_at=booking.starts_at,
                data=BookingAttributedData(
                    booking_id=booking.id,
                    starts_at=booking.starts_at,
                    price=booking.price,
                    status=booking.status,
                    billing_month=f"{local:%Y-%m}",
                ),
            )
            if await record_event(session, event, dedupe_key=f"booking.attributed:{booking.id}"):
                attributed += 1
        await session.commit()
    if attributed:
        logger.info("bookings_attributed", count=attributed)
    return attributed


@celery.task(name="periodic.attribute_visits")
def attribute_visits_task() -> int:
    return run(attribute_visits(runtime().session_factory))
