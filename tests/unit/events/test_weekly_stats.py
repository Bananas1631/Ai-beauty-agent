"""Правила недельной статистики: что считается и в какую неделю попадает событие."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from beauty_agent.db.models import Salon
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.events import BookingCreated, BookingData, LlmCallFinished, LlmCallFinishedData
from beauty_agent.events.handlers.reports import increments

SALON = Salon(id=uuid.uuid4(), slug="s", name="Салон", timezone="Europe/Moscow", settings={})


def answer_at(moment: datetime) -> LlmCallFinished:
    return LlmCallFinished(
        salon_id=SALON.id,
        occurred_at=moment,
        data=LlmCallFinishedData(
            conversation_id=uuid.uuid4(),
            prompt_version="v2",
            prompt_tokens=1,
            completion_tokens=1,
            cost_rub=Decimal("0.05"),
            latency_ms=1,
            tool_calls=0,
        ),
    )


def booking(created_by_ai: bool) -> BookingCreated:
    moment = datetime(2026, 12, 14, 10, tzinfo=UTC)
    return BookingCreated(
        salon_id=SALON.id,
        data=BookingData(
            booking_id=uuid.uuid4(),
            service_id=uuid.uuid4(),
            master_id=uuid.uuid4(),
            starts_at=moment,
            ends_at=moment,
            status=BookingStatus.CONFIRMED,
            created_by_ai=created_by_ai,
        ),
    )


@pytest.mark.parametrize(
    ("moment", "off_hours"),
    [
        (datetime(2026, 12, 14, 6, 59, tzinfo=UTC), 1),  # 09:59 МСК — до открытия (с 10:00)
        (datetime(2026, 12, 14, 7, 0, tzinfo=UTC), 0),  # 10:00 МСК
        (datetime(2026, 12, 14, 17, 59, tzinfo=UTC), 0),  # 20:59 МСК
        (datetime(2026, 12, 14, 18, 0, tzinfo=UTC), 1),  # 21:00 МСК — уже вне рабочего времени
    ],
)
def test_off_hours_by_salon_time(moment: datetime, off_hours: int) -> None:
    assert increments(answer_at(moment), SALON) == {
        "answers": 1,
        "off_hours_answers": off_hours,
        "llm_cost_rub": Decimal("0.05"),
    }


def test_only_ai_bookings_count() -> None:
    assert increments(booking(created_by_ai=True), SALON) == {"ai_bookings": 1}
    assert increments(booking(created_by_ai=False), SALON) == {}
