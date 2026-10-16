"""Kafka в контейнере (KRaft, один узел) и база с настоящими коммитами для тестов событий.

Внешний порт контейнера совпадает с внутренним: Kafka сообщает клиенту адрес из
advertised.listeners, и localhost:<порт> должен вести туда же и с хоста.
Тесты relay работают с настоящими транзакциями, а не внутри откатываемой: параллельные relay
должны конкурировать за блокировки из разных соединений. Свои салоны тест удаляет сам.
"""

import json
import socket
import time
import uuid
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from aiokafka import AIOKafkaConsumer
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from beauty_agent.core.config import KafkaSettings, PostgresSettings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.db.models import EventOutbox, ProcessedEvent, Salon, SalonWeekStats
from beauty_agent.events.producer import DLQ_SUFFIX, EventProducer, ensure_topics

KAFKA_IMAGE = "apache/kafka:3.9.1"  # тот же образ, что в docker-compose


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(scope="session")
def kafka() -> Iterator[KafkaSettings]:
    port = free_port()
    container = (
        DockerContainer(KAFKA_IMAGE)
        .with_bind_ports(port, port)
        .with_env("KAFKA_NODE_ID", "1")
        .with_env("KAFKA_PROCESS_ROLES", "broker,controller")
        .with_env("KAFKA_LISTENERS", f"PLAINTEXT://:{port},CONTROLLER://:9093")
        .with_env("KAFKA_ADVERTISED_LISTENERS", f"PLAINTEXT://localhost:{port}")
        .with_env("KAFKA_LISTENER_SECURITY_PROTOCOL_MAP", "PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT")
        .with_env("KAFKA_CONTROLLER_LISTENER_NAMES", "CONTROLLER")
        .with_env("KAFKA_CONTROLLER_QUORUM_VOTERS", "1@localhost:9093")
        .with_env("KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR", "1")
        .with_env("KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", "1")
        .with_env("KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS", "0")
        .with_env("KAFKA_AUTO_CREATE_TOPICS_ENABLE", "false")
        .with_env("KAFKA_HEAP_OPTS", "-Xms256m -Xmx512m")
        .waiting_for(LogMessageWaitStrategy("Kafka Server started").with_startup_timeout(120))
    )
    with container:
        yield KafkaSettings(bootstrap_servers=f"localhost:{port}", client_id="beauty-tests", request_timeout_ms=15_000)


@dataclass
class CommittedDb:
    """Сессии с настоящими коммитами; салоны, созданные через salons(), удаляются после теста."""

    factory: async_sessionmaker[AsyncSession]
    created: list[uuid.UUID] = field(default_factory=list)

    async def salons(self, count: int) -> list[uuid.UUID]:
        salons = [Salon(slug=f"events-{uuid.uuid4().hex[:8]}", name="Салон событий") for _ in range(count)]
        async with self.factory() as session:
            session.add_all(salons)
            await session.commit()
        self.created.extend(salon.id for salon in salons)
        return [salon.id for salon in salons]


@pytest.fixture
async def committed(migrated: PostgresSettings) -> AsyncIterator[CommittedDb]:
    engine = make_engine(migrated)
    db = CommittedDb(make_session_factory(engine))
    try:
        yield db
    finally:
        async with db.factory() as session:
            for model in (EventOutbox, ProcessedEvent, SalonWeekStats):
                await session.execute(delete(model).where(model.salon_id.in_(db.created)))
            await session.execute(delete(Salon).where(Salon.id.in_(db.created)))
            await session.commit()
        await engine.dispose()


async def consume(
    settings: KafkaSettings, topic: str, salon_ids: Iterable[uuid.UUID], expected: int, *, timeout_s: float = 30
) -> list[dict[str, Any]]:
    """Сообщения топика этих салонов по порядку партиций и offset — как их прочитает потребитель."""
    wanted = {str(salon_id) for salon_id in salon_ids}
    consumer = AIOKafkaConsumer(
        topic,
        bootstrap_servers=settings.bootstrap_servers,
        group_id=None,
        auto_offset_reset="earliest",
        enable_auto_commit=False,
    )
    await consumer.start()
    found: list[tuple[int, int, dict[str, Any]]] = []
    deadline = time.monotonic() + timeout_s
    try:
        while len(found) < expected and time.monotonic() < deadline:
            batch = await consumer.getmany(timeout_ms=500)
            for partition, messages in batch.items():
                for message in messages:
                    payload = json.loads(message.value)
                    if payload["salon_id"] in wanted:
                        payload["_key"] = message.key.decode()
                        found.append((partition.partition, message.offset, payload))
    finally:
        await consumer.stop()
    return [payload for _, _, payload in sorted(found, key=lambda item: (item[0], item[1]))]


@pytest.fixture
async def topic(kafka: KafkaSettings) -> str:
    name = f"test.events.{uuid.uuid4().hex[:8]}"
    await ensure_topics(kafka, [name, f"{name}{DLQ_SUFFIX}"])
    return name


@pytest.fixture
async def producer(kafka: KafkaSettings) -> AsyncIterator[EventProducer]:
    started = EventProducer(kafka)
    await started.start()
    yield started
    await started.stop()
