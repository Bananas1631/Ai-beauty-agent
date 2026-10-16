"""Relay event_outbox → Kafka на настоящих PostgreSQL и Kafka.

- порядок событий салона сохраняется, ключ сообщения — salon_id;
- падение между публикацией и отметкой: событие уходит повторно, но не теряется;
- два relay одновременно не ломают порядок и не дублируют;
- топики создаются с 6 партициями и сроком хранения 7 дней.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from aiokafka.admin import AIOKafkaAdminClient
from aiokafka.admin.config_resource import ConfigResource, ConfigResourceType
from sqlalchemy import func, select
from tests.integration.events.conftest import CommittedDb, consume

from beauty_agent.core.config import KafkaSettings
from beauty_agent.db.models import EventOutbox
from beauty_agent.db.models.event_outbox import PENDING
from beauty_agent.domain.enums import Channel
from beauty_agent.domain.events import MessageReceived, MessageReceivedData
from beauty_agent.events.outbox import record_event
from beauty_agent.events.outbox_relay import OutboxRelay
from beauty_agent.events.producer import EventProducer, ensure_topics

TOPIC = "beauty.messages"


async def write_events(db: CommittedDb, salons: Sequence[uuid.UUID], per_salon: int) -> dict[str, list[str]]:
    """События вперемешку по салонам, каждое своей транзакцией — как в жизни."""
    written: dict[str, list[str]] = {str(salon): [] for salon in salons}
    for _ in range(per_salon):
        for salon in salons:
            event = MessageReceived(
                salon_id=salon,
                data=MessageReceivedData(
                    message_id=uuid.uuid4(),
                    conversation_id=uuid.uuid4(),
                    client_id=uuid.uuid4(),
                    channel=Channel.TELEGRAM,
                ),
            )
            async with db.factory() as session:
                await record_event(session, event)
                await session.commit()
            written[str(salon)].append(str(event.event_id))
    return written


def first_seen_order(messages: Sequence[dict[str, object]]) -> dict[str, list[str]]:
    order: dict[str, list[str]] = {}
    for message in messages:
        events = order.setdefault(str(message["salon_id"]), [])
        if message["event_id"] not in events:
            events.append(str(message["event_id"]))
    return order


async def drain(relay: OutboxRelay) -> None:
    for _ in range(200):
        if not await relay.run_once() and not await relay.pending_salons():
            return
        await asyncio.sleep(0)
    raise AssertionError("relay не опубликовал outbox")


@pytest.fixture
async def producer(kafka: KafkaSettings) -> AsyncIterator[EventProducer]:
    await ensure_topics(kafka)
    started = EventProducer(kafka)
    await started.start()
    yield started
    await started.stop()


async def pending(db: CommittedDb) -> int:
    async with db.factory() as session:
        statement = select(func.count()).select_from(EventOutbox).where(EventOutbox.salon_id.in_(db.created), PENDING)
        return int(await session.scalar(statement) or 0)


async def test_order_is_kept_per_salon(kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer) -> None:
    salons = await committed.salons(3)
    written = await write_events(committed, salons, per_salon=20)

    await drain(OutboxRelay(committed.factory, producer, batch=8))
    messages = await consume(kafka, TOPIC, salons, expected=60)

    assert len(messages) == 60
    assert first_seen_order(messages) == written
    assert all(message["_key"] == message["salon_id"] for message in messages)  # ключ — салон
    assert await pending(committed) == 0


async def test_crash_between_publish_and_mark_repeats_not_loses(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, monkeypatch: pytest.MonkeyPatch
) -> None:
    [salon] = await committed.salons(1)
    written = await write_events(committed, [salon], per_salon=5)
    relay = OutboxRelay(committed.factory, producer)
    original = relay.mark_published

    async def crash(*_: object) -> None:
        raise ConnectionResetError("relay убит после публикации")

    monkeypatch.setattr(relay, "mark_published", crash)
    with pytest.raises(ConnectionResetError):
        await relay.run_once()
    still_pending = await pending(committed)
    monkeypatch.setattr(relay, "mark_published", original)
    await drain(relay)
    messages = await consume(kafka, TOPIC, [salon], expected=10)

    assert still_pending == 5  # отметка откатилась — события остались в outbox
    assert len(messages) == 10  # каждое событие дважды: дубль, а не потеря
    assert first_seen_order(messages) == written
    assert await pending(committed) == 0


async def test_two_relays_neither_reorder_nor_duplicate(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer
) -> None:
    salons = await committed.salons(4)
    written = await write_events(committed, salons, per_salon=15)
    first = OutboxRelay(committed.factory, producer, batch=4)
    second = OutboxRelay(committed.factory, producer, batch=4)

    await asyncio.gather(drain(first), drain(second))
    messages = await consume(kafka, TOPIC, salons, expected=60)

    assert len(messages) == 60
    assert first_seen_order(messages) == written


async def test_topics_have_partitions_and_retention(kafka: KafkaSettings, producer: EventProducer) -> None:
    admin = AIOKafkaAdminClient(bootstrap_servers=kafka.bootstrap_servers)
    await admin.start()
    try:
        topics = await admin.describe_topics(["beauty.bookings", "beauty.llm.dlq"])
        [response] = await admin.describe_configs([ConfigResource(ConfigResourceType.TOPIC, "beauty.bookings")])
    finally:
        await admin.close()
    configs = {entry[0]: entry[1] for entry in response.resources[0][4]}

    assert {topic["topic"]: len(topic["partitions"]) for topic in topics} == {"beauty.bookings": 6, "beauty.llm.dlq": 6}
    assert configs["retention.ms"] == str(7 * 24 * 3600 * 1000)
