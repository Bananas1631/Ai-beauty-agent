"""Процесс потребителей событий: python -m beauty_agent.events.consumers weekly-stats [knowledge-sync …]

Одна группа — один процесс (сервис consumers в docker-compose); для параллельности запускают
несколько экземпляров той же группы — Kafka раздаст им партиции и перераспределит при остановке.
DLQ и уведомления администратору — через тот же Runtime, что у воркеров.

Группы: weekly-stats — недельная статистика салона; knowledge-sync — результат переиндексации из
индексатора (ADR 014); partner-webhooks — подписанные вебхуки партнёрам о записях.
"""

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable, Sequence

import structlog

from beauty_agent.adapters.webhooks_out import WebhookSender
from beauty_agent.core import metrics
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.events.consumer import EventConsumer, EventHandler
from beauty_agent.events.handlers.knowledge import KnowledgeSyncHandler
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.handlers.webhooks import PartnerWebhooksHandler
from beauty_agent.events.producer import EventProducer, ensure_topics
from beauty_agent.rag.safe_http import make_safe_client
from beauty_agent.runtime import Runtime, create_runtime

logger = structlog.get_logger(__name__)


def _partner_webhooks(runtime: Runtime) -> EventHandler:
    # адрес партнёра — внешний: только через клиент без SSRF (внутренние адреса недоступны)
    return PartnerWebhooksHandler(WebhookSender(make_safe_client(timeout_s=10.0), runtime.security))


HANDLERS: dict[str, Callable[[Runtime], EventHandler]] = {
    "weekly-stats": lambda _runtime: WeeklyStatsHandler(),
    "knowledge-sync": lambda runtime: KnowledgeSyncHandler(runtime.redis),
    "partner-webhooks": _partner_webhooks,
}


async def main_async(names: Sequence[str]) -> None:
    """Одна или несколько групп в одном процессе: у каждой свой потребитель и своя позиция чтения."""
    settings = get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    runtime = create_runtime(settings)
    handlers = [HANDLERS[name](runtime) for name in names]
    dlq = EventProducer(settings.kafka)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows: остановка по Ctrl+C
            loop.add_signal_handler(sig, stop.set)
    metrics.serve_process_metrics()
    await ensure_topics(settings.kafka)
    await dlq.start()
    consumers = [
        EventConsumer(settings.kafka, runtime.session_factory, handler, dlq=dlq, notifier=runtime.notifier)
        for handler in handlers
    ]
    for consumer in consumers:
        await consumer.start()
        logger.info("consumer_started", group=consumer.handler.group, topics=list(consumer.handler.topics))
    try:
        await asyncio.gather(*(consumer.run(stop) for consumer in consumers))
    finally:
        for consumer in consumers:
            await consumer.stop()  # отдать партиции группе сразу, а не по таймауту сессии
        await dlq.stop()
        await runtime.close()
        logger.info("consumer_stopped", groups=[handler.group for handler in handlers])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Потребители доменных событий")
    parser.add_argument("handlers", nargs="+", choices=sorted(HANDLERS))
    args = parser.parse_args(argv)
    asyncio.run(main_async(list(dict.fromkeys(args.handlers))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
