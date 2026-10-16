"""Идемпотентный продюсер Kafka и создание топиков.

- enable_idempotence + acks=all: повтор отправки внутри продюсера не создаёт дубль в партиции,
  а подтверждение приходит только после записи на все реплики in-sync.
- Ключ сообщения — salon_id: события салона попадают в одну партицию и читаются по порядку.
- Значение — JSON конверта события (domain/events.py); тип, версия схемы и event_id — ещё и в
  заголовках, чтобы потребитель мог отсеять чужое, не разбирая тело.
- Топики создаются с явными партициями и сроком хранения: автосоздание в брокере выключено,
  иначе опечатка в имени топика молча создала бы новый с настройками по умолчанию.
"""

import asyncio
import contextlib
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from aiokafka import AIOKafkaProducer
from aiokafka.admin import AIOKafkaAdminClient, NewTopic
from aiokafka.errors import TopicAlreadyExistsError

from beauty_agent.core.config import KafkaSettings
from beauty_agent.domain.events import TOPICS

logger = structlog.get_logger(__name__)

DLQ_SUFFIX = ".dlq"


@dataclass(frozen=True, slots=True)
class OutgoingEvent:
    topic: str
    key: str  # salon_id
    value: bytes
    event_type: str
    event_id: str
    schema_version: int


def all_topics() -> list[str]:
    topics = sorted(set(TOPICS.values()))
    return topics + [f"{topic}{DLQ_SUFFIX}" for topic in topics]


async def ensure_topics(settings: KafkaSettings, topics: Sequence[str] | None = None) -> None:
    """Создать недостающие топики: 6 партиций, хранение 7 дней. Существующие не меняются."""
    admin = AIOKafkaAdminClient(
        bootstrap_servers=settings.bootstrap_servers,
        client_id=f"{settings.client_id}-admin",
        request_timeout_ms=settings.request_timeout_ms,
    )
    await admin.start()
    try:
        existing = set(await admin.list_topics())
        missing = [topic for topic in (topics or all_topics()) if topic not in existing]
        if not missing:
            return
        retention_ms = str(settings.retention_days * 24 * 3600 * 1000)
        # другой экземпляр relay мог создать их одновременно с нами
        with contextlib.suppress(TopicAlreadyExistsError):
            await admin.create_topics(
                [
                    NewTopic(
                        name=topic,
                        num_partitions=settings.partitions,
                        replication_factor=settings.replication_factor,
                        topic_configs={"retention.ms": retention_ms, "cleanup.policy": "delete"},
                    )
                    for topic in missing
                ]
            )
        logger.info("kafka_topics_created", topics=missing)
    finally:
        await admin.close()


class EventProducer:
    def __init__(self, settings: KafkaSettings) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=settings.bootstrap_servers,
            client_id=settings.client_id,
            enable_idempotence=True,
            acks="all",
            linger_ms=5,
            request_timeout_ms=settings.request_timeout_ms,
        )

    async def start(self) -> None:
        await self._producer.start()

    async def stop(self) -> None:
        await self._producer.stop()

    async def send_raw(self, topic: str, key: bytes | None, value: bytes, headers: list[tuple[str, bytes]]) -> None:
        """Сообщение как есть — для DLQ: тело и заголовки исходного события плюс причина."""
        await self._producer.send_and_wait(topic, value=value, key=key, headers=headers)

    async def publish(self, events: Sequence[OutgoingEvent]) -> None:
        """Отправить пачку и дождаться подтверждения каждого события.

        Отправка поставлена в очередь продюсера по порядку, а идемпотентный продюсер сохраняет
        порядок внутри партиции и при повторах. Ошибка любого события — исключение: relay
        откатит транзакцию и опубликует пачку снова (потребители отсеют дубли по event_id).
        """
        futures = []
        for event in events:
            futures.append(
                await self._producer.send(
                    event.topic,
                    value=event.value,
                    key=event.key.encode(),
                    headers=[
                        ("event_type", event.event_type.encode()),
                        ("event_id", event.event_id.encode()),
                        ("schema_version", str(event.schema_version).encode()),
                    ],
                )
            )
        await asyncio.gather(*futures)
