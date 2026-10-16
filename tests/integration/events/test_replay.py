"""Перечитывание журнала: новый потребитель строит данные за всё время, повтор не удваивает статистику."""

import asyncio
import time
from datetime import UTC, datetime

from sqlalchemy import func, select
from tests.channel_helpers import RecordingNotifier
from tests.integration.events.conftest import CommittedDb
from tests.integration.events.test_consumer import CountingHandler, consume_until, message_received, publish

from beauty_agent.core.config import KafkaSettings
from beauty_agent.db.models import SalonWeekStats
from beauty_agent.events.consumer import EventConsumer, EventHandler, PollResult
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.producer import EventProducer


async def replay(
    kafka: KafkaSettings,
    db: CommittedDb,
    handler: EventHandler,
    producer: EventProducer,
    group: str,
    *,
    since: datetime | None = None,
) -> PollResult:
    consumer = EventConsumer(
        kafka, db.factory, handler, dlq=producer, notifier=RecordingNotifier(), subscribe=False, kafka_group=group
    )
    await consumer.start()
    try:
        partitions = await consumer.assign_all()
        await consumer.seek(partitions, since=since)
        total = PollResult()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            total += await consumer.poll_once(timeout_ms=500)
            if await consumer.caught_up():
                return total
        raise AssertionError("перечитывание не дошло до конца топика")
    finally:
        await consumer.stop()


async def messages_counted(db: CommittedDb) -> int:
    async with db.factory() as session:
        statement = select(func.coalesce(func.sum(SalonWeekStats.messages_received), 0)).where(
            SalonWeekStats.salon_id.in_(db.created)
        )
        return int(await session.scalar(statement) or 0)


async def test_new_consumer_reads_everything_retained(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    events = [message_received(salon) for _ in range(10)]
    await publish(producer, topic, *events)
    live = CountingHandler(f"live-{topic}", [topic])
    consumer = EventConsumer(kafka, committed.factory, live, dlq=producer, retry_delay_s=0.01)
    await consumer.start()
    try:
        await consume_until(consumer, 10)
    finally:
        await consumer.stop()

    loader = CountingHandler(f"clickhouse-{topic}", [topic])  # новый потребитель, пришедший позже
    result = await replay(kafka, committed, loader, producer, f"clickhouse-{topic}")

    assert result.handled == 10
    assert loader.seen == live.seen == [event.event_id for event in events]


async def test_replaying_same_handler_does_not_double_stats(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    await publish(producer, topic, *(message_received(salon) for _ in range(5)))
    handler = WeeklyStatsHandler(group=f"stats-{topic}", topics=[topic])
    consumer = EventConsumer(kafka, committed.factory, handler, dlq=producer, retry_delay_s=0.01)
    await consumer.start()
    try:
        await consume_until(consumer, 5)
    finally:
        await consumer.stop()

    result = await replay(kafka, committed, handler, producer, f"stats-{topic}-replay")

    assert (result.handled, result.duplicates) == (0, 5)
    assert await messages_counted(committed) == 5  # не 10


async def test_replay_from_moment(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    await publish(producer, topic, *(message_received(salon) for _ in range(3)))
    await asyncio.sleep(1.5)
    moment = datetime.now(UTC)
    later = [message_received(salon) for _ in range(2)]
    await publish(producer, topic, *later)
    handler = CountingHandler(f"since-{topic}", [topic])

    result = await replay(kafka, committed, handler, producer, f"since-{topic}", since=moment)

    assert result.handled == 2
    assert handler.seen == [event.event_id for event in later]
