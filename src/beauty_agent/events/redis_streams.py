"""ЭКСПЕРИМЕНТ (ветка experiment/redis-streams, в main не вливается): тот же потребитель на Redis Streams.

Сравнение с Kafka — docs/adr/013-kafka-and-rabbitmq.md, раздел «Эксперимент: Redis Streams».

- Поток на топик (beauty.bookings…), группы — XGROUP; новое — XREADGROUP «>», подтверждение — XACK
  после обработки; идемпотентность та же: processed_events в одной транзакции с результатом.
- Повтор: неподтверждённое сообщение остаётся в pending; XAUTOCLAIM забирает его у упавшего
  потребителя через min_idle_ms и увеличивает счётчик доставок. На max_attempts — в <поток>:dlq.
- Хранение — по времени: XADD с MINID (~7 дней), как retention.ms у Kafka, но в памяти Redis.
- Порядка внутри салона при нескольких потребителях группы нет: сообщения раздаются по одному,
  а не партициями по ключу.
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.domain.events import parse_event
from beauty_agent.events.consumer import EventHandler, PollResult, claim
from beauty_agent.events.producer import OutgoingEvent

logger = structlog.get_logger(__name__)

DLQ_SUFFIX = ":dlq"
RETENTION_MS = 7 * 24 * 3600 * 1000


class RedisStreamPublisher:
    """Замена Kafka-продюсера для relay: XADD в поток топика с обрезкой старше 7 дней."""

    def __init__(self, redis: Redis, *, retention_ms: int = RETENTION_MS) -> None:
        self.redis = redis
        self.retention_ms = retention_ms

    async def publish(self, events: Sequence[OutgoingEvent]) -> None:
        oldest = f"{int(time.time() * 1000) - self.retention_ms}-0"
        for event in events:
            await self.redis.xadd(
                event.topic,
                {"event": event.value, "key": event.key, "event_type": event.event_type, "event_id": event.event_id},
                minid=oldest,
                approximate=True,
            )


@dataclass
class StreamMessage:
    stream: str
    message_id: str
    fields: dict[str, str]


class RedisStreamConsumer:
    def __init__(
        self,
        redis: Redis,
        session_factory: async_sessionmaker[AsyncSession],
        handler: EventHandler,
        *,
        consumer_name: str,
        max_attempts: int = 3,
        batch: int = 100,
        block_ms: int = 500,
        min_idle_ms: int = 30_000,
    ) -> None:
        self.redis = redis
        self.session_factory = session_factory
        self.handler = handler
        self.name = consumer_name
        self.max_attempts = max_attempts
        self.batch = batch
        self.block_ms = block_ms
        self.min_idle_ms = min_idle_ms

    async def ensure_groups(self) -> None:
        for stream in self.handler.topics:
            try:
                await self.redis.xgroup_create(stream, self.handler.group, id="0", mkstream=True)
            except ResponseError as exc:
                if "BUSYGROUP" not in str(exc):  # группа уже есть — это нормально
                    raise

    async def poll_once(self) -> PollResult:
        result = PollResult()
        for message in await self._reclaimed() + await self._new():
            result += await self._process(message)
        return result

    async def _new(self) -> list[StreamMessage]:
        response: Any = await self.redis.xreadgroup(
            self.handler.group,
            self.name,
            dict.fromkeys(self.handler.topics, ">"),
            count=self.batch,
            block=self.block_ms,
        )
        return [
            StreamMessage(_text(stream), _text(message_id), _fields(fields))
            for stream, messages in response or []
            for message_id, fields in messages
        ]

    async def _reclaimed(self) -> list[StreamMessage]:
        """Неподтверждённые дольше min_idle_ms — у упавшего потребителя или после ошибки обработчика."""
        found: list[StreamMessage] = []
        for stream in self.handler.topics:
            reply: Any = await self.redis.xautoclaim(
                stream, self.handler.group, self.name, self.min_idle_ms, start_id="0-0", count=self.batch
            )
            found += [StreamMessage(stream, _text(message_id), _fields(fields)) for message_id, fields in reply[1]]
        return found

    async def _deliveries(self, message: StreamMessage) -> int:
        pending = await self.redis.xpending_range(
            message.stream, self.handler.group, min=message.message_id, max=message.message_id, count=1
        )
        return int(pending[0]["times_delivered"]) if pending else 1

    async def _process(self, message: StreamMessage) -> PollResult:
        try:
            event = parse_event(message.fields["event"])
        except (ValidationError, KeyError):
            await self._dead_letter(message, "не по схеме")
            return PollResult(dead_lettered=1)
        try:
            async with self.session_factory() as session, session.begin():
                fresh = await claim(session, self.handler.group, event)
                if fresh:
                    await self.handler.handle(session, event)
        except Exception as exc:
            if await self._deliveries(message) >= self.max_attempts:
                await self._dead_letter(message, type(exc).__name__)
                return PollResult(dead_lettered=1)
            logger.warning("stream_handler_failed", group=self.handler.group, error=type(exc).__name__)
            return PollResult()  # останется в pending: XAUTOCLAIM доставит снова
        await self.redis.xack(message.stream, self.handler.group, message.message_id)
        return PollResult(handled=1) if fresh else PollResult(duplicates=1)

    async def _dead_letter(self, message: StreamMessage, reason: str) -> None:
        fields: dict[FieldT, EncodableT] = {name: value for name, value in message.fields.items()}
        fields.update({"dlq_reason": reason, "dlq_group": self.handler.group, "dlq_source": message.message_id})
        await self.redis.xadd(f"{message.stream}{DLQ_SUFFIX}", fields)
        await self.redis.xack(message.stream, self.handler.group, message.message_id)


def _text(value: object) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _fields(raw: dict[object, object]) -> dict[str, str]:
    return {_text(name): _text(value) for name, value in raw.items()}
