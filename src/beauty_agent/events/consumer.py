"""Базовый потребитель событий Kafka: пачки, commit после обработки, идемпотентность и DLQ.

- Читает пачками (getmany), offset коммитит вручную и только после обработки всех событий
  партиции из пачки: упавший посреди пачки процесс перечитает её после перезапуска.
- Повторная доставка не даёт двойного эффекта: event_id пишется в processed_events (обработчик +
  event_id уникальны) в той же транзакции, что и результат обработчика. Дубль — пропуск, в том
  числе при перечитывании журнала другой группой Kafka (scripts/replay_events.py).
- Ошибка обработчика — до 3 попыток с паузой; затем событие уходит в <топик>.dlq с причиной в
  заголовках, администратор получает уведомление, а потребитель идёт дальше: одно «ядовитое»
  событие не останавливает партицию. Событие не по схеме — сразу в DLQ, повтор не поможет.
- Каждая группа потребителей читает топик независимо: отчёты, аналитика и будущий сервис
  на Kotlin получают одни и те же события, а продюсер о них не знает.
- Lag (сколько событий группа ещё не прочитала) — метрика Prometheus по группе и топику.
- Отметка об обработке — claim: по умолчанию processed_events основного сервиса; у индексатора
  своя таблица в своей схеме (ADR 014), остальная логика та же.
"""

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import structlog
from aiokafka import AIOKafkaConsumer, ConsumerRecord, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.core import metrics
from beauty_agent.core.config import KafkaSettings
from beauty_agent.db.base import uuid7
from beauty_agent.db.models import ProcessedEvent
from beauty_agent.domain.events import Event, parse_event
from beauty_agent.events.producer import DLQ_SUFFIX

logger = structlog.get_logger(__name__)

MAX_ATTEMPTS = 3


class EventHandler(Protocol):
    @property
    def group(self) -> str: ...

    @property
    def topics(self) -> Sequence[str]: ...

    async def handle(self, session: AsyncSession, event: Event) -> None:
        """Записать результат в session; commit сделает потребитель вместе с отметкой о событии."""
        ...


class DeadLetterSink(Protocol):
    async def send_raw(self, topic: str, key: bytes | None, value: bytes, headers: list[tuple[str, bytes]]) -> None: ...


@dataclass
class PollResult:
    handled: int = 0
    duplicates: int = 0
    dead_lettered: int = 0

    def __iadd__(self, other: "PollResult") -> "PollResult":
        self.handled += other.handled
        self.duplicates += other.duplicates
        self.dead_lettered += other.dead_lettered
        return self


Claim = Callable[[AsyncSession, str, Event], Awaitable[bool]]


async def claim(session: AsyncSession, group: str, event: Event) -> bool:
    """Отметить событие обработанным этой группой; False — уже обработано (дубль)."""
    statement = (
        insert(ProcessedEvent)
        .values(id=uuid7(), salon_id=event.salon_id, consumer_group=group, event_id=event.event_id)
        .on_conflict_do_nothing(index_elements=["consumer_group", "event_id"])
        .returning(ProcessedEvent.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


class EventConsumer:
    def __init__(
        self,
        settings: KafkaSettings,
        session_factory: async_sessionmaker[AsyncSession],
        handler: EventHandler,
        *,
        dlq: DeadLetterSink,
        notifier: Notifier | None = None,
        max_attempts: int = MAX_ATTEMPTS,
        retry_delay_s: float = 0.5,
        batch_max: int = 100,
        subscribe: bool = True,
        kafka_group: str | None = None,
        claim: Claim = claim,
        max_poll_interval_ms: int = 300_000,
    ) -> None:
        """subscribe=False — партиции назначаются вручную (assign_all): перечитывание одним процессом.

        kafka_group — своя группа Kafka (своя позиция чтения) для перечитывания. Идемпотентность при
        этом остаётся за обработчиком (handler.group в processed_events): перечитывание применит только
        то, что ещё не применено, и не удвоит статистику.
        """
        self.settings = settings
        self.session_factory = session_factory
        self.handler = handler
        self.dlq = dlq
        self.notifier = notifier
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self.batch_max = batch_max
        self.claim = claim
        self.consumer = AIOKafkaConsumer(
            *(handler.topics if subscribe else ()),
            bootstrap_servers=settings.bootstrap_servers,
            client_id=f"{settings.client_id}-{kafka_group or handler.group}",
            group_id=kafka_group or handler.group,
            enable_auto_commit=False,
            auto_offset_reset="earliest",  # новая группа читает всё, что хранится в топике
            max_poll_records=batch_max,
            max_poll_interval_ms=max_poll_interval_ms,
            request_timeout_ms=settings.request_timeout_ms,
        )

    async def start(self) -> None:
        await self.consumer.start()

    async def stop(self) -> None:
        await self.consumer.stop()

    async def assign_all(self) -> list[TopicPartition]:
        """Все партиции топиков обработчика — этому процессу, без ребалансировки группы."""
        # partitions_for_topic потребителя знает только топики подписки — спрашиваем кластер напрямую
        admin = AIOKafkaAdminClient(
            bootstrap_servers=self.settings.bootstrap_servers, request_timeout_ms=self.settings.request_timeout_ms
        )
        await admin.start()
        try:
            described = await admin.describe_topics(list(self.handler.topics))
        finally:
            await admin.close()
        partitions = [
            TopicPartition(topic["topic"], partition["partition"])
            for topic in described
            for partition in sorted(topic["partitions"], key=lambda item: item["partition"])
        ]
        self.consumer.assign(partitions)
        return partitions

    async def seek(
        self,
        partitions: Sequence[TopicPartition],
        *,
        since: datetime | None = None,
        offsets: dict[TopicPartition, int] | None = None,
    ) -> None:
        """С начала хранения (по умолчанию), с момента времени или с заданных offset."""
        if offsets is not None:
            for partition, offset in offsets.items():
                self.consumer.seek(partition, offset)
            return
        if since is None:
            await self.consumer.seek_to_beginning(*partitions)
            return
        moment_ms = int(since.timestamp() * 1000)
        found = await self.consumer.offsets_for_times({partition: moment_ms for partition in partitions})
        for partition in partitions:
            position = found.get(partition)
            if position is None:  # после этого момента событий нет — читать с конца
                await self.consumer.seek_to_end(partition)
            else:
                self.consumer.seek(partition, position.offset)

    async def caught_up(self) -> bool:
        """Группа дочитала всё, что было в партициях на момент проверки."""
        for partition in self.consumer.assignment():
            end = (await self.consumer.end_offsets([partition]))[partition]
            if await self.consumer.position(partition) < end:
                return False
        return True

    async def poll_once(self, timeout_ms: int = 1000) -> PollResult:
        result = PollResult()
        batch: dict[TopicPartition, list[ConsumerRecord[bytes, bytes]]] = await self.consumer.getmany(
            timeout_ms=timeout_ms, max_records=self.batch_max
        )
        for partition, records in batch.items():
            for record in records:
                result += await self._process(record)
            # offset — следующего непрочитанного: после перезапуска группа продолжит отсюда
            await self.consumer.commit({partition: records[-1].offset + 1})
        await self._report_lag()
        return result

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self.poll_once()
            except Exception as exc:  # БД или Kafka недоступны: offset не закоммичен, пачка придёт снова
                logger.warning("consumer_poll_failed", group=self.handler.group, error=type(exc).__name__)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=5)

    async def _process(self, record: ConsumerRecord[bytes, bytes]) -> PollResult:
        try:
            event = parse_event(record.value)
        except ValidationError as exc:
            await self._dead_letter(record, None, f"не по схеме: {exc.error_count()} ошибок")
            return PollResult(dead_lettered=1)
        error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self.session_factory() as session, session.begin():
                    if not await self.claim(session, self.handler.group, event):
                        return PollResult(duplicates=1)
                    await self.handler.handle(session, event)
                return PollResult(handled=1)
            except Exception as exc:  # транзакция откатилась вместе с отметкой — можно повторить
                error = exc
                logger.warning(
                    "event_handler_failed",
                    group=self.handler.group,
                    event_type=event.type,
                    attempt=attempt,
                    error=type(exc).__name__,
                )
                if attempt < self.max_attempts:
                    await asyncio.sleep(self.retry_delay_s * attempt)
        await self._dead_letter(record, event, type(error).__name__ if error else "ошибка")
        return PollResult(dead_lettered=1)

    async def _dead_letter(self, record: ConsumerRecord[bytes, bytes], event: Event | None, reason: str) -> None:
        headers: list[tuple[str, bytes]] = [*(record.headers or ())]
        headers += [
            ("dlq_reason", reason.encode()),
            ("dlq_group", self.handler.group.encode()),
            ("dlq_source", f"{record.topic}:{record.partition}:{record.offset}".encode()),
        ]
        await self.dlq.send_raw(f"{record.topic}{DLQ_SUFFIX}", record.key, record.value, headers)
        metrics.event_dead_lettered(self.handler.group, record.topic)
        logger.error("event_dead_lettered", group=self.handler.group, topic=record.topic, reason=reason)
        if self.notifier is not None:
            what = event.type if event is not None else "без схемы"
            await self.notifier.notify(
                AdminEvent(
                    AdminEventKind.DEAD_LETTER,
                    str(event.salon_id) if event is not None else "-",
                    None,
                    f"Событие {what} не обработано ({self.handler.group}): {reason}",
                )
            )

    async def _report_lag(self) -> None:
        lag: dict[str, int] = {}
        for partition in self.consumer.assignment():
            highwater = self.consumer.highwater(partition)
            if highwater is None:
                continue
            position = await self.consumer.position(partition)
            lag[partition.topic] = lag.get(partition.topic, 0) + max(highwater - position, 0)
        for topic, value in lag.items():
            metrics.consumer_lag(self.handler.group, topic, value)


def headers_dict(headers: Sequence[tuple[str, bytes]] | None) -> dict[str, str]:
    return {name: value.decode() for name, value in headers or ()}
