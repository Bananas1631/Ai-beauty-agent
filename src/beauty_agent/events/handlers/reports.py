"""Потребитель статистики для еженедельного отчёта салону (группа weekly-stats).

Вместо ночных SQL-запросов по сообщениям и черновикам всех салонов статистика копится по мере
событий: строка salon_week_stats на салон и неделю (с понедельника, по времени салона).
Сервис записи и сервис диалога не знают об этом потребителе — они только публикуют события.

- booking.created (запись ИИ) → ai_bookings; booking.cancelled → cancelled_bookings;
- booking.attributed → attributed_visits (визит наступил, запись засчитана ИИ);
- message.received → messages_received;
- llm.call.finished → answers, off_hours_answers (вне рабочего времени администраторов) и llm_cost_rub.

Увеличение — одним UPSERT: параллельные потребители разных партиций не теряют обновлений.
"""

from collections.abc import Sequence
from datetime import timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Salon, SalonWeekStats
from beauty_agent.domain.events import (
    STATS_TOPICS,
    BookingAttributed,
    BookingCancelled,
    BookingCreated,
    Event,
    LlmCallFinished,
    MessageReceived,
)

GROUP = "weekly-stats"


def increments(event: Event, salon: Salon) -> dict[str, int | Decimal]:
    match event:
        case BookingCreated() if event.data.created_by_ai:
            return {"ai_bookings": 1}
        case BookingCancelled():
            return {"cancelled_bookings": 1}
        case BookingAttributed():
            return {"attributed_visits": 1}
        case MessageReceived():
            return {"messages_received": 1}
        case LlmCallFinished():
            config = salon.config
            local = event.occurred_at.astimezone(ZoneInfo(salon.timezone)).time()
            off_hours = local < config.work_start or local >= config.work_end
            return {"answers": 1, "off_hours_answers": int(off_hours), "llm_cost_rub": event.data.cost_rub}
    return {}


class WeeklyStatsHandler:
    def __init__(self, *, group: str = GROUP, topics: Sequence[str] | None = None) -> None:
        self._group = group
        self._topics = tuple(topics or STATS_TOPICS)

    @property
    def group(self) -> str:
        return self._group

    @property
    def topics(self) -> Sequence[str]:
        return self._topics

    async def handle(self, session: AsyncSession, event: Event) -> None:
        salon = await session.get(Salon, event.salon_id)
        if salon is None:
            return
        values = increments(event, salon)
        if not values:
            return
        local_day = event.occurred_at.astimezone(ZoneInfo(salon.timezone)).date()
        week_start = local_day - timedelta(days=local_day.weekday())
        statement = insert(SalonWeekStats).values(salon_id=salon.id, week_start=week_start, **values)
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["salon_id", "week_start"],
                set_={
                    **{name: getattr(SalonWeekStats, name) + statement.excluded[name] for name in values},
                    "updated_at": func.now(),
                },
            )
        )
