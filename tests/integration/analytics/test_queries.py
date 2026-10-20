"""Запросы аналитики на подготовленных строках: расходы по моделям, p95, воронка, неделя салона."""

import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.analytics.clickhouse import ClickHouse
from beauty_agent.analytics.queries import Analytics, funnel_steps
from beauty_agent.db.models import Salon
from beauty_agent.workers.reports import weekly_report

COLUMNS = (
    "event_id",
    "event_type",
    "schema_version",
    "salon_id",
    "ts",
    "conversation_id",
    "booking_id",
    "created_by_ai",
    "model",
    "cost_rub",
    "latency_ms",
    "offers_shown",
)
START = datetime(2026, 12, 1, tzinfo=UTC)


def row(
    salon_id: uuid.UUID,
    event_type: str,
    ts: datetime,
    *,
    conversation: uuid.UUID | None = None,
    model: str = "",
    cost: str = "0",
    latency: int = 0,
    offers: int = 0,
    by_ai: bool = False,
    event_id: uuid.UUID | None = None,
) -> list[Any]:
    return [
        event_id or uuid.uuid4(),
        event_type,
        1,
        salon_id,
        ts,
        conversation,
        None,
        by_ai,
        model,
        Decimal(cost),
        latency,
        offers,
    ]


async def insert(clickhouse: ClickHouse, rows: list[list[Any]]) -> None:
    await clickhouse.insert("analytics.events", rows, COLUMNS)


async def test_cost_by_salon_and_model_counts_duplicates_once(clickhouse: ClickHouse) -> None:
    salon = uuid.uuid4()
    duplicate = uuid.uuid4()
    moment = START + timedelta(days=3)
    await insert(
        clickhouse,
        [
            row(salon, "llm.call.finished", moment, model="qwen3-235b", cost="0.40", event_id=duplicate),
            row(salon, "llm.call.finished", moment, model="qwen3-235b", cost="0.40", event_id=duplicate),
            row(salon, "llm.call.finished", moment, model="gigachat-2-lite", cost="1.10"),
            row(salon, "llm.call.finished", moment, model="", cost="0"),  # ответ из кэша
            row(salon, "llm.call.finished", START - timedelta(days=1), model="qwen3-235b", cost="9"),  # не тот месяц
        ],
    )

    costs = [
        cost
        for cost in await Analytics(clickhouse).cost_by_salon_and_model(START, START + timedelta(days=31))
        if cost.salon_id == salon
    ]

    assert [(cost.model, cost.answers, cost.cost_rub) for cost in costs] == [
        ("gigachat-2-lite", 1, Decimal("1.1000")),
        ("qwen3-235b", 1, Decimal("0.4000")),
        ("без модели", 1, Decimal("0.0000")),
    ]


async def test_p95_latency_by_local_day(clickhouse: ClickHouse) -> None:
    salon = uuid.uuid4()
    # 23:30 UTC 1 декабря — уже 2 декабря в Москве
    late = datetime(2026, 12, 1, 23, 30, tzinfo=UTC)
    await insert(
        clickhouse,
        [row(salon, "llm.call.finished", late, model="m", latency=ms) for ms in range(100, 2100, 100)]
        + [row(salon, "llm.call.finished", late, model="", latency=5)],  # без модели — не в задержке модели
    )

    (day,) = await Analytics(clickhouse).latency_by_day(
        salon, START, START + timedelta(days=7), timezone="Europe/Moscow"
    )

    assert day.day == "2026-12-02"
    assert day.answers == 20
    assert 1800 <= day.p95_ms <= 2000
    assert 900 <= day.p50_ms <= 1100


async def test_booking_funnel(clickhouse: ClickHouse) -> None:
    salon = uuid.uuid4()
    rows: list[list[Any]] = []
    for index in range(10):  # 10 написали
        conversation = uuid.uuid4()
        moment = START + timedelta(days=1, minutes=index)
        rows.append(row(salon, "message.received", moment, conversation=conversation))
        if index < 6:  # 6 увидели варианты
            rows.append(
                row(
                    salon,
                    "llm.call.finished",
                    moment + timedelta(seconds=5),
                    conversation=conversation,
                    model="m",
                    offers=3,
                )
            )
        if index < 4:  # 4 записались
            rows.append(
                row(salon, "booking.created", moment + timedelta(minutes=2), conversation=conversation, by_ai=True)
            )
        if index < 3:  # 3 пришли — визит через 5 дней, после конца периода
            rows.append(row(salon, "booking.attributed", moment + timedelta(days=5), conversation=conversation))
    # запись без показанных вариантов — шаги идут по порядку, дальше первого она не проходит
    stray = uuid.uuid4()
    rows += [
        row(salon, "message.received", START + timedelta(days=1), conversation=stray),
        row(salon, "booking.created", START + timedelta(days=1, minutes=1), conversation=stray, by_ai=True),
    ]
    await insert(clickhouse, rows)

    steps = await Analytics(clickhouse).booking_funnel(salon, START, START + timedelta(days=2))

    assert [(step.name, step.conversations) for step in steps] == [
        ("Написали", 11),
        ("Показаны варианты", 6),
        ("Записались через ИИ", 4),
        ("Пришли на визит", 3),
    ]
    assert steps[3].share_of_first == round(3 / 11, 3)


def test_funnel_steps_from_levels() -> None:
    steps = funnel_steps({1: 5, 2: 2, 4: 3})

    assert [step.conversations for step in steps] == [10, 5, 3, 3]
    assert funnel_steps({})[0].share_of_first == 0.0


async def test_week_stats_with_off_hours_in_salon_time(clickhouse: ClickHouse) -> None:
    salon = uuid.uuid4()
    # работа с 10:00 до 20:00 по Москве (UTC+3): 06:59 UTC = 09:59 МСК — вне времени, 07:00 UTC — в рабочее
    await insert(
        clickhouse,
        [
            row(salon, "llm.call.finished", datetime(2026, 12, 7, 6, 59, tzinfo=UTC), model="m", cost="0.30"),
            row(salon, "llm.call.finished", datetime(2026, 12, 7, 7, 0, tzinfo=UTC), model="m", cost="0.20"),
            row(salon, "llm.call.finished", datetime(2026, 12, 7, 17, 0, tzinfo=UTC), model="m", cost="0.10"),
            row(salon, "booking.created", datetime(2026, 12, 7, 12, 0, tzinfo=UTC), by_ai=True),
            row(salon, "booking.created", datetime(2026, 12, 7, 12, 5, tzinfo=UTC), by_ai=False),
        ],
    )

    stats = await Analytics(clickhouse).week_stats(
        salon,
        datetime(2026, 12, 7, tzinfo=UTC),
        datetime(2026, 12, 14, tzinfo=UTC),
        timezone="Europe/Moscow",
        work_start=time(10),
        work_end=time(20),
    )

    assert stats.ai_bookings == 1
    assert stats.answers == 3
    assert stats.off_hours_answers == 2  # 09:59 и 20:00 по Москве
    assert stats.llm_cost_rub == Decimal("0.6000")


async def test_weekly_report_takes_numbers_from_clickhouse(
    clickhouse: ClickHouse, session: AsyncSession, salon: Salon
) -> None:
    monday = datetime(2026, 12, 14, 9, 0, tzinfo=UTC)  # отчёт за неделю 7–13 декабря
    await insert(
        clickhouse,
        [
            row(salon.id, "booking.created", datetime(2026, 12, 8, 12, tzinfo=UTC), by_ai=True),
            row(salon.id, "llm.call.finished", datetime(2026, 12, 8, 12, tzinfo=UTC), model="m", cost="0.25"),
            row(
                salon.id, "llm.call.finished", datetime(2026, 12, 14, 8, tzinfo=UTC), model="m", cost="5"
            ),  # уже эта неделя
        ],
    )

    report = await weekly_report(session, salon, monday, analytics=Analytics(clickhouse))

    assert report.ai_bookings == 1
    assert report.answers == 1
    assert report.llm_cost_rub == Decimal("0.2500")
    assert "Записей через ИИ: 1" in report.text()
