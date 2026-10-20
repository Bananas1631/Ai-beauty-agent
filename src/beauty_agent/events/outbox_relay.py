"""Relay: event_outbox → Kafka. Запуск: python -m beauty_agent.events.outbox_relay (сервис relay).

Каждый проход — по салонам с неопубликованными событиями, каждый салон своей транзакцией:

1. pg_try_advisory_xact_lock по салону: салон в работе у другого экземпляра — пропускаем.
   SKIP LOCKED по строкам не хватило бы: второй relay взял бы более поздние события того же
   салона и опубликовал их раньше первого — порядок внутри салона сломался бы.
2. SELECT … ORDER BY id … FOR UPDATE SKIP LOCKED — пачка событий салона по порядку записи.
3. Публикация идемпотентным продюсером и ожидание подтверждений (acks=all).
4. UPDATE published_at и COMMIT — блокировки снимаются вместе с транзакцией.

Падение между 3 и 4 — транзакция откатывается, события публикуются снова: дубль, но не потеря
(at-least-once). Потребители отсеивают дубли по event_id (events/consumer.py).

Опубликованное старше срока хранения Kafka удаляется: перечитать его можно только из Kafka,
а пока оно там — из outbox тоже. Неопубликованное не удаляется никогда.

Тот же relay публикует outbox индексатора (table=… из его схемы knowledge, ADR 014).
"""

import asyncio
import contextlib
import json
import signal
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core import metrics
from beauty_agent.core.config import KafkaSettings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.models import EventOutbox
from beauty_agent.db.models.event_outbox import PENDING, OutboxColumns
from beauty_agent.events.producer import EventProducer, OutgoingEvent, ensure_topics

logger = structlog.get_logger(__name__)

LOCK_NAMESPACE = 0x0E7E  # первый ключ advisory-блокировки: «relay событий», второй — хеш салона
SALONS_PER_ROUND = 50
PURGE_EVERY = 600  # проходов (~5 минут при паузе 0,5 с)


class Publisher(Protocol):
    async def publish(self, events: Sequence[OutgoingEvent]) -> None: ...


def outgoing(row: OutboxColumns) -> OutgoingEvent:
    return OutgoingEvent(
        topic=row.topic,
        key=str(row.salon_id),
        value=_json_bytes(row.payload),
        event_type=row.event_type,
        event_id=str(row.event_id),
        schema_version=int(row.payload.get("schema_version", 1)),
    )


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()


class OutboxRelay:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: Publisher,
        *,
        batch: int = 200,
        retention: timedelta = timedelta(days=7),
        table: type[OutboxColumns] = EventOutbox,
        lock_namespace: int = LOCK_NAMESPACE,
    ) -> None:
        self.session_factory = session_factory
        self.publisher = publisher
        self.batch = batch
        self.retention = retention
        self.table = table
        self.lock_namespace = lock_namespace

    async def pending_salons(self) -> list[uuid.UUID]:
        """Салоны с неопубликованными событиями — начиная с самого давнего события."""
        async with self.session_factory() as session:
            table = self.table
            rows = await session.scalars(
                select(table.salon_id)
                .where(PENDING)
                .group_by(table.salon_id)
                .order_by(func.min(table.id))
                .limit(SALONS_PER_ROUND)
            )
            return list(rows)

    async def publish_salon(self, salon_id: uuid.UUID) -> int:
        async with self.session_factory() as session, session.begin():
            locked = await session.scalar(
                select(func.pg_try_advisory_xact_lock(self.lock_namespace, func.hashtext(str(salon_id))))
            )
            if not locked:
                return 0  # салон публикует другой экземпляр relay
            table = self.table
            rows = list(
                await session.scalars(
                    select(table)
                    .where(table.salon_id == salon_id, PENDING)
                    .order_by(table.id)
                    .limit(self.batch)
                    .with_for_update(skip_locked=True)
                )
            )
            if not rows:
                return 0
            await self.publisher.publish([outgoing(row) for row in rows])
            await self.mark_published(session, [row.id for row in rows])
        metrics.events_published(len(rows))
        return len(rows)

    async def mark_published(self, session: AsyncSession, ids: Sequence[int]) -> None:
        await session.execute(update(self.table).where(self.table.id.in_(ids)).values(published_at=func.now()))

    async def run_once(self) -> int:
        published = 0
        for salon_id in await self.pending_salons():
            published += await self.publish_salon(salon_id)
        return published

    async def backlog(self) -> int:
        async with self.session_factory() as session:
            return int(await session.scalar(select(func.count()).select_from(self.table).where(PENDING)) or 0)

    async def purge(self, now: datetime | None = None) -> int:
        cutoff = (now or datetime.now(UTC)) - self.retention
        async with self.session_factory() as session:
            table = self.table
            deleted = await session.execute(
                delete(table).where(table.published_at.is_not(None), table.published_at < cutoff).returning(table.id)
            )
            count = len(deleted.all())
            await session.commit()
        return count

    async def run(self, stop: asyncio.Event, *, idle_s: float = 0.5) -> None:
        rounds = 0
        while not stop.is_set():
            try:
                published = await self.run_once()
                metrics.outbox_backlog(await self.backlog())
                rounds += 1
                if rounds % PURGE_EVERY == 0:
                    await self.purge()
            except Exception as exc:  # Kafka или БД недоступны: события ждут в outbox, пробуем снова
                logger.warning("relay_round_failed", error=type(exc).__name__)
                published = 0
                await asyncio.sleep(min(idle_s * 10, 5.0))
            if not published:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_s)


async def main_async(settings: KafkaSettings | None = None) -> None:
    app_settings = get_settings()
    kafka = settings or app_settings.kafka
    configure_logging(app_settings.app.log_level, json_logs=app_settings.app.log_json)
    engine = make_engine(app_settings.postgres)
    producer = EventProducer(kafka)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows: остановка по Ctrl+C
            loop.add_signal_handler(sig, stop.set)
    metrics.serve_process_metrics()
    await ensure_topics(kafka)
    await producer.start()
    logger.info("relay_started", bootstrap=kafka.bootstrap_servers)
    try:
        relay = OutboxRelay(
            make_session_factory(engine),
            producer,
            batch=kafka.relay_batch,
            retention=timedelta(days=kafka.retention_days),
        )
        await relay.run(stop, idle_s=kafka.relay_idle_s)
    finally:
        await producer.stop()
        await engine.dispose()
        logger.info("relay_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
