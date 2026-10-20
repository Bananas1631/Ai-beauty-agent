"""Воркер индексатора: python -m beauty_indexer.worker

Три цикла в одном процессе:
1. потребитель Kafka (группа indexer, топик beauty.knowledge) — site.reindex.requested → задача;
2. исполнитель задач — обход сайта и knowledge.updated в outbox;
3. relay своего outbox — публикация knowledge.updated в Kafka.

Экземпляров может быть несколько: партиции делит Kafka, задачи — SKIP LOCKED, салоны relay —
advisory-блокировка (со своим пространством ключей, чтобы не пересекаться с relay основного сервиса).
"""

import asyncio
import contextlib
import signal
from datetime import timedelta

import structlog

from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.core import metrics
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.events.consumer import EventConsumer
from beauty_agent.events.outbox_relay import OutboxRelay
from beauty_agent.events.producer import EventProducer, ensure_topics
from beauty_indexer.config import get_settings
from beauty_indexer.db import EventOutbox
from beauty_indexer.jobs import JobRunner, ReindexRequests, claim

logger = structlog.get_logger(__name__)

RELAY_LOCK_NAMESPACE = 0x1D3E  # свой первый ключ advisory-блокировки: relay индексатора


async def main_async() -> None:
    settings = get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    engine = make_engine(settings.postgres)
    session_factory = make_session_factory(engine)
    producer = EventProducer(settings.kafka)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows: остановка по Ctrl+C
            loop.add_signal_handler(sig, stop.set)
    metrics.serve_process_metrics()
    await ensure_topics(settings.kafka)
    await producer.start()
    handler = ReindexRequests(group=settings.indexer.consumer_group, max_pages=settings.indexer.max_pages)
    consumer = EventConsumer(settings.kafka, session_factory, handler, dlq=producer, claim=claim)
    runner = JobRunner(
        session_factory,
        build_embedder(settings.llm),
        pause_s=settings.indexer.pause_s,
        stale_after=timedelta(seconds=settings.indexer.stale_job_s),
    )
    relay = OutboxRelay(
        session_factory,
        producer,
        batch=settings.kafka.relay_batch,
        retention=timedelta(days=settings.kafka.retention_days),
        table=EventOutbox,
        lock_namespace=RELAY_LOCK_NAMESPACE,
    )
    await consumer.start()
    logger.info("indexer_worker_started", group=handler.group)
    try:
        await asyncio.gather(
            consumer.run(stop),
            runner.run(stop, idle_s=settings.indexer.job_poll_s),
            relay.run(stop, idle_s=settings.kafka.relay_idle_s),
        )
    finally:
        await consumer.stop()
        await producer.stop()
        await engine.dispose()
        logger.info("indexer_worker_stopped")


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
