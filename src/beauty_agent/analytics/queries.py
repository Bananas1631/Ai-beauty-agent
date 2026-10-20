"""Аналитика в ClickHouse: расходы на LLM, задержка ответа, воронка записи, недельная статистика.

- Деньги и счёт — с FINAL: дубли доставки at-least-once схлопнуты на чтении, а не «когда-нибудь».
  FINAL дороже обычного чтения (слияние на лету), но в пределах салона и месяца это миллисекунды.
- Квантили — quantile (приближённый, reservoir sampling): для дашборда p95 точности хватает.
- Воронка — windowFunnel по диалогу: сообщение клиента → показаны варианты времени → запись ИИ →
  визит засчитан. Окно — 14 дней: столько обычно проходит от вопроса до визита.
- Время в отчётах салона — в его часовом поясе: день и неделя считаются по местному времени.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal

from beauty_agent.analytics.clickhouse import ClickHouse

FUNNEL_WINDOW_S = 14 * 24 * 3600
FUNNEL_STEPS = ("Написали", "Показаны варианты", "Записались через ИИ", "Пришли на визит")


@dataclass(frozen=True, slots=True)
class ModelCost:
    salon_id: uuid.UUID
    model: str
    answers: int
    tokens: int
    cost_rub: Decimal


@dataclass(frozen=True, slots=True)
class DailyLatency:
    day: str
    answers: int
    p50_ms: int
    p95_ms: int


@dataclass(frozen=True, slots=True)
class FunnelStep:
    name: str
    conversations: int
    share_of_first: float  # доля от тех, кто написал


@dataclass(frozen=True, slots=True)
class WeekStats:
    ai_bookings: int
    answers: int
    off_hours_answers: int
    llm_cost_rub: Decimal


COST_BY_SALON_AND_MODEL = """
SELECT salon_id, if(model = '', 'без модели', model) AS model, count() AS answers,
       sum(prompt_tokens + completion_tokens) AS tokens, sum(cost_rub) AS cost_rub
FROM analytics.events FINAL
WHERE event_type = 'llm.call.finished' AND ts >= {start:DateTime64(3)} AND ts < {end:DateTime64(3)}
GROUP BY salon_id, model
ORDER BY cost_rub DESC, salon_id, model
"""

LATENCY_BY_DAY = """
SELECT toString(toDate(ts, {tz:String})) AS day, count() AS answers,
       toUInt32(quantile(0.5)(latency_ms)) AS p50_ms, toUInt32(quantile(0.95)(latency_ms)) AS p95_ms
FROM analytics.events
WHERE salon_id = {salon:UUID} AND event_type = 'llm.call.finished' AND model != ''
  AND ts >= {start:DateTime64(3)} AND ts < {end:DateTime64(3)}
GROUP BY day
ORDER BY day
"""

# Запись засчитана (booking.attributed) приходит с временем визита, поэтому верхняя граница
# для неё — не конец периода, а конец периода плюс окно воронки
BOOKING_FUNNEL = """
SELECT level, count() AS conversations
FROM
(
    SELECT conversation_id,
           windowFunnel({window:UInt64})(
               toDateTime(ts),
               event_type = 'message.received',
               event_type = 'llm.call.finished' AND offers_shown > 0,
               event_type = 'booking.created' AND created_by_ai,
               event_type = 'booking.attributed'
           ) AS level
    FROM analytics.events FINAL
    WHERE salon_id = {salon:UUID} AND conversation_id IS NOT NULL
      AND ts >= {start:DateTime64(3)} AND ts < {end:DateTime64(3)} + toIntervalSecond({window:UInt64})
    GROUP BY conversation_id
    HAVING countIf(event_type = 'message.received' AND ts < {end:DateTime64(3)}) > 0
)
GROUP BY level
ORDER BY level
"""

SALON_LLM_COST = """
SELECT sum(cost_rub)
FROM analytics.events FINAL
WHERE salon_id = {salon:UUID} AND event_type = 'llm.call.finished'
  AND ts >= {start:DateTime64(3)} AND ts < {end:DateTime64(3)}
"""

WEEK_STATS = """
SELECT
    countIf(event_type = 'booking.created' AND created_by_ai) AS ai_bookings,
    countIf(event_type = 'llm.call.finished') AS answers,
    countIf(event_type = 'llm.call.finished' AND (
        local_second < {work_start:UInt32} OR local_second >= {work_end:UInt32}
    )) AS off_hours_answers,
    sumIf(cost_rub, event_type = 'llm.call.finished') AS llm_cost_rub
FROM
(
    SELECT *, toHour(toTimeZone(ts, {tz:String})) * 3600 + toMinute(toTimeZone(ts, {tz:String})) * 60
              + toSecond(ts) AS local_second
    FROM analytics.events FINAL
    WHERE salon_id = {salon:UUID} AND ts >= {start:DateTime64(3)} AND ts < {end:DateTime64(3)}
)
"""


def _seconds_of_day(moment: time) -> int:
    """Секунда суток — сравнение времени, не зависящее от версии ClickHouse (тип Time появился недавно)."""
    return moment.hour * 3600 + moment.minute * 60 + moment.second


class Analytics:
    def __init__(self, clickhouse: ClickHouse) -> None:
        self.ch = clickhouse

    async def cost_by_salon_and_model(self, start: datetime, end: datetime) -> list[ModelCost]:
        rows = await self.ch.rows(COST_BY_SALON_AND_MODEL, {"start": start, "end": end})
        return [
            ModelCost(
                salon_id=row["salon_id"],
                model=str(row["model"]),
                answers=int(row["answers"]),
                tokens=int(row["tokens"]),
                cost_rub=Decimal(row["cost_rub"]),
            )
            for row in rows
        ]

    async def salon_llm_cost(self, salon_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
        value = await self.ch.scalar(SALON_LLM_COST, {"salon": salon_id, "start": start, "end": end})
        return Decimal(value or 0)

    async def latency_by_day(
        self, salon_id: uuid.UUID, start: datetime, end: datetime, *, timezone: str
    ) -> list[DailyLatency]:
        rows = await self.ch.rows(LATENCY_BY_DAY, {"salon": salon_id, "start": start, "end": end, "tz": timezone})
        return [
            DailyLatency(str(row["day"]), int(row["answers"]), int(row["p50_ms"]), int(row["p95_ms"])) for row in rows
        ]

    async def booking_funnel(self, salon_id: uuid.UUID, start: datetime, end: datetime) -> list[FunnelStep]:
        rows = await self.ch.rows(
            BOOKING_FUNNEL, {"salon": salon_id, "start": start, "end": end, "window": FUNNEL_WINDOW_S}
        )
        return funnel_steps({int(row["level"]): int(row["conversations"]) for row in rows})

    async def week_stats(
        self, salon_id: uuid.UUID, start: datetime, end: datetime, *, timezone: str, work_start: time, work_end: time
    ) -> WeekStats:
        (row,) = await self.ch.rows(
            WEEK_STATS,
            {
                "salon": salon_id,
                "start": start,
                "end": end,
                "tz": timezone,
                "work_start": _seconds_of_day(work_start),
                "work_end": _seconds_of_day(work_end),
            },
        )
        return WeekStats(
            ai_bookings=int(row["ai_bookings"]),
            answers=int(row["answers"]),
            off_hours_answers=int(row["off_hours_answers"]),
            llm_cost_rub=Decimal(row["llm_cost_rub"]),
        )


def funnel_steps(by_level: dict[int, int]) -> list[FunnelStep]:
    """windowFunnel отдаёт для диалога глубину — сколько шагов он прошёл; шаг k прошли все с level ≥ k."""
    passed = [sum(count for level, count in by_level.items() if level >= step) for step in range(1, 5)]
    first = passed[0] or 1
    return [FunnelStep(name, count, round(count / first, 3)) for name, count in zip(FUNNEL_STEPS, passed, strict=True)]
