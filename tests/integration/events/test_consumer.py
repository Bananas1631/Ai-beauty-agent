"""Потребитель событий на настоящей Kafka: идемпотентность, DLQ, независимые группы, недельная статистика."""

import time
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from aiokafka import AIOKafkaConsumer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import RecordingNotifier
from tests.integration.events.conftest import CommittedDb

from beauty_agent.conversation.notifications import AdminEventKind
from beauty_agent.core.config import KafkaSettings
from beauty_agent.db.models import Salon, SalonWeekStats
from beauty_agent.domain.enums import Channel
from beauty_agent.domain.events import (
    DomainEvent,
    Event,
    LlmCallFinished,
    LlmCallFinishedData,
    MessageReceived,
    MessageReceivedData,
)
from beauty_agent.events.consumer import EventConsumer, PollResult, headers_dict
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.producer import DLQ_SUFFIX, EventProducer, OutgoingEvent
from beauty_agent.workers.reports import weekly_report

MONDAY_EVENING = datetime(2026, 12, 14, 19, 30, tzinfo=UTC)  # 22:30 МСК — вне рабочего времени


class CountingHandler:
    """Обработчик для тестов: запоминает события, падает на «ядовитых»."""

    def __init__(self, group: str, topics: Sequence[str], poisoned: set[uuid.UUID] | None = None) -> None:
        self.group = group
        self.topics = topics
        self.poisoned = poisoned or set()
        self.seen: list[uuid.UUID] = []

    async def handle(self, session: AsyncSession, event: Event) -> None:
        if event.event_id in self.poisoned:
            raise ValueError("обработчик не справился")
        self.seen.append(event.event_id)


async def publish(producer: EventProducer, topic: str, *events: DomainEvent) -> None:
    await producer.publish(
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


async def consume_until(consumer: EventConsumer, wanted: int, *, timeout_s: float = 30) -> PollResult:
    total = PollResult()
    deadline = time.monotonic() + timeout_s
    while total.handled + total.duplicates + total.dead_lettered < wanted and time.monotonic() < deadline:
        total += await consumer.poll_once(timeout_ms=500)
    return total


def message_received(salon: uuid.UUID) -> MessageReceived:
    return MessageReceived(
        salon_id=salon,
        occurred_at=MONDAY_EVENING,
        data=MessageReceivedData(
            message_id=uuid.uuid4(), conversation_id=uuid.uuid4(), client_id=uuid.uuid4(), channel=Channel.TELEGRAM
        ),
    )


def llm_finished(salon: uuid.UUID, cost: str = "0.12") -> LlmCallFinished:
    return LlmCallFinished(
        salon_id=salon,
        occurred_at=MONDAY_EVENING,
        data=LlmCallFinishedData(
            conversation_id=uuid.uuid4(),
            model="qwen3-235b",
            prompt_version="v2",
            prompt_tokens=900,
            completion_tokens=80,
            cost_rub=Decimal(cost),
            latency_ms=1200,
            tool_calls=1,
        ),
    )


def consumer_for(
    kafka: KafkaSettings, db: CommittedDb, handler: object, producer: EventProducer, notifier: RecordingNotifier
) -> EventConsumer:
    return EventConsumer(kafka, db.factory, handler, dlq=producer, notifier=notifier, retry_delay_s=0.01)  # type: ignore[arg-type]


async def test_duplicate_does_not_double_weekly_stats(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    answer = llm_finished(salon)
    await publish(producer, topic, message_received(salon), answer, answer)  # relay повторил событие
    consumer = consumer_for(
        kafka, committed, WeeklyStatsHandler(group=f"stats-{topic}", topics=[topic]), producer, RecordingNotifier()
    )
    await consumer.start()
    try:
        result = await consume_until(consumer, 3)
    finally:
        await consumer.stop()
    async with committed.factory() as session:
        stats = await session.scalar(select(SalonWeekStats).where(SalonWeekStats.salon_id == salon))
        salon_row = await session.get(Salon, salon)
        assert salon_row is not None
        report = await weekly_report(session, salon_row, datetime(2026, 12, 21, 9, 0, tzinfo=UTC), from_events=True)

    assert (result.handled, result.duplicates) == (2, 1)
    assert stats is not None
    assert stats.week_start.isoformat() == "2026-12-14"  # неделя с понедельника по времени салона
    assert (stats.messages_received, stats.answers, stats.off_hours_answers) == (1, 1, 1)
    assert stats.llm_cost_rub == Decimal("0.12")  # не 0.24: дубль не удвоил
    assert (report.answers, report.off_hours_answers, report.llm_cost_rub) == (1, 1, Decimal("0.12"))


async def test_failing_event_goes_to_dlq_and_partition_moves_on(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    poisoned, good = message_received(salon), message_received(salon)
    handler = CountingHandler(f"dlq-{topic}", [topic], poisoned={poisoned.event_id})
    notifier = RecordingNotifier()
    await publish(producer, topic, poisoned, good)
    await producer.send_raw(topic, str(salon).encode(), b"{not json", [])  # тот же ключ — та же партиция
    consumer = consumer_for(kafka, committed, handler, producer, notifier)
    await consumer.start()
    try:
        result = await consume_until(consumer, 3)
    finally:
        await consumer.stop()
    restarted = consumer_for(kafka, committed, handler, producer, notifier)
    await restarted.start()
    try:
        again = await consume_until(restarted, 1, timeout_s=5)
    finally:
        await restarted.stop()
    dead = await read_dlq(kafka, f"{topic}{DLQ_SUFFIX}", 2)

    assert (result.handled, result.dead_lettered) == (1, 2)
    assert handler.seen == [good.event_id]  # после «ядовитого» события партиция не встала
    assert again == PollResult()  # offset закоммичен: ни одно событие не пришло снова
    reasons = [headers_dict(headers)["dlq_reason"] for _, headers in dead]
    assert reasons[0] == "ValueError"
    assert reasons[1].startswith("не по схеме")
    assert all(headers_dict(headers)["dlq_group"] == handler.group for _, headers in dead)
    assert dead[0][0] == poisoned.model_dump_json().encode()  # в DLQ — исходное тело события
    assert [event.kind for event in notifier.events] == [AdminEventKind.DEAD_LETTER] * 2


async def test_two_groups_read_the_same_event(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    [salon] = await committed.salons(1)
    event = message_received(salon)
    await publish(producer, topic, event)
    reports, analytics = CountingHandler(f"reports-{topic}", [topic]), CountingHandler(f"analytics-{topic}", [topic])

    for handler in (reports, analytics):
        consumer = consumer_for(kafka, committed, handler, producer, RecordingNotifier())
        await consumer.start()
        try:
            await consume_until(consumer, 1)
        finally:
            await consumer.stop()

    assert reports.seen == analytics.seen == [event.event_id]


async def read_dlq(kafka: KafkaSettings, topic: str, expected: int) -> list[tuple[bytes, list[tuple[str, bytes]]]]:
    consumer = AIOKafkaConsumer(
        topic, bootstrap_servers=kafka.bootstrap_servers, group_id=None, auto_offset_reset="earliest"
    )
    await consumer.start()
    found: list[tuple[int, int, bytes, list[tuple[str, bytes]]]] = []
    deadline = time.monotonic() + 20
    try:
        while len(found) < expected and time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for partition, messages in batch.items():
                found += [(partition.partition, m.offset, m.value, list(m.headers)) for m in messages]
    finally:
        await consumer.stop()
    return [(value, headers) for _, _, value, headers in sorted(found, key=lambda item: (item[0], item[1]))]
