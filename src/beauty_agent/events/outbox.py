"""Запись доменного события в event_outbox — в транзакции вызывающего кода, без commit.

Вызывается рядом с изменением данных: запись создана — booking.created в той же транзакции.
Откат транзакции убирает и событие: в Kafka не уйдёт то, чего в БД нет.
"""

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import EventOutbox
from beauty_agent.domain.events import DomainEvent, topic_of


async def record_event(session: AsyncSession, event: DomainEvent, *, dedupe_key: str | None = None) -> bool:
    """False — событие с этим dedupe_key уже записано (оно должно случиться один раз)."""
    payload = event.model_dump(mode="json")
    statement = (
        insert(EventOutbox)
        .values(
            event_id=event.event_id,
            salon_id=event.salon_id,
            event_type=payload["type"],
            topic=topic_of(event),
            payload=payload,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=["dedupe_key"])
        .returning(EventOutbox.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None
