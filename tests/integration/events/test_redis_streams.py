"""ЭКСПЕРИМЕНТ: тот же потребитель на Redis Streams — что совпадает с Kafka и чем отличается."""

import uuid
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select
from tests.integration.events.conftest import CommittedDb
from tests.integration.events.test_consumer import CountingHandler, message_received

from beauty_agent.db.models import SalonWeekStats
from beauty_agent.domain.events import DomainEvent
from beauty_agent.events.consumer import PollResult
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.producer import OutgoingEvent
from beauty_agent.events.redis_streams import DLQ_SUFFIX, RedisStreamConsumer, RedisStreamPublisher


def stream() -> str:
    return f"test.stream.{uuid.uuid4().hex[:8]}"


async def publish(redis: Redis, topic: str, *events: DomainEvent) -> None:
    await RedisStreamPublisher(redis).publish(
        [
            OutgoingEvent(
                topic=topic,
                key=str(event.salon_id),
                value=event.model_dump_json().encode(),
                event_type=str(event.model_dump(include={"type"})["type"]),
                event_id=str(event.event_id),
                schema_version=event.schema_version,
            )
            for event in events
        ]
    )


async def drain(consumer: RedisStreamConsumer, rounds: int = 10) -> PollResult:
    total = PollResult()
    for _ in range(rounds):
        total += await consumer.poll_once()
    return total


async def test_duplicate_is_not_counted_twice(redis: Redis, committed: CommittedDb) -> None:
    [salon] = await committed.salons(1)
    topic = stream()
    event = message_received(salon)
    await publish(redis, topic, event, event)
    consumer = RedisStreamConsumer(
        redis, committed.factory, WeeklyStatsHandler(group=f"stats-{topic}", topics=[topic]), consumer_name="a"
    )
    await consumer.ensure_groups()

    result = await drain(consumer, 2)
    async with committed.factory() as session:
        stats = await session.scalar(select(SalonWeekStats).where(SalonWeekStats.salon_id == salon))

    assert (result.handled, result.duplicates) == (1, 1)
    assert stats is not None
    assert stats.messages_received == 1


async def test_poisoned_event_goes_to_dlq_after_three_deliveries(redis: Redis, committed: CommittedDb) -> None:
    [salon] = await committed.salons(1)
    topic = stream()
    poisoned, good = message_received(salon), message_received(salon)
    await publish(redis, topic, poisoned, good)
    handler = CountingHandler(f"dlq-{topic}", [topic], poisoned={poisoned.event_id})
    consumer = RedisStreamConsumer(redis, committed.factory, handler, consumer_name="a", min_idle_ms=0)
    await consumer.ensure_groups()

    result = await drain(consumer, 5)
    dead: Any = await redis.xrange(f"{topic}{DLQ_SUFFIX}")

    assert (result.handled, result.dead_lettered) == (1, 1)
    assert handler.seen == [good.event_id]
    assert len(dead) == 1
    assert dead[0][1]["dlq_reason"] == "ValueError"
    assert await redis.xpending(topic, handler.group) == {
        "pending": 0,
        "min": None,
        "max": None,
        "consumers": [],
    }  # всё подтверждено


async def test_groups_are_independent_but_order_per_salon_is_not_kept(redis: Redis, committed: CommittedDb) -> None:
    [salon] = await committed.salons(1)
    topic = stream()
    events = [message_received(salon) for _ in range(20)]
    await publish(redis, topic, *events)
    first, second = CountingHandler(f"g1-{topic}", [topic]), CountingHandler(f"g1-{topic}", [topic])
    other_group = CountingHandler(f"g2-{topic}", [topic])
    consumers = [
        RedisStreamConsumer(redis, committed.factory, handler, consumer_name=name, batch=5)
        for handler, name in ((first, "a"), (second, "b"), (other_group, "c"))
    ]
    for consumer in consumers:
        await consumer.ensure_groups()

    for _ in range(4):  # два потребителя одной группы читают по очереди
        await consumers[0].poll_once()
        await consumers[1].poll_once()
    await drain(consumers[2], 5)

    assert sorted(first.seen + second.seen) == sorted(event.event_id for event in events)
    # события одного салона разошлись по двум потребителям: порядок внутри салона держится только
    # при одном потребителе группы (у Kafka — партиция на ключ)
    assert first.seen
    assert second.seen
    assert other_group.seen == [event.event_id for event in events]  # другая группа читает всё сама
