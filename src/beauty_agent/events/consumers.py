"""Процесс потребителя событий: python -m beauty_agent.events.consumers weekly-stats

Одна группа — один процесс (сервис consumers в docker-compose); для параллельности запускают
несколько экземпляров той же группы — Kafka раздаст им партиции и перераспределит при остановке.
DLQ и уведомления администратору — через тот же Runtime, что у воркеров.
"""

import argparse
import asyncio
import contextlib
import signal
import sys
from collections.abc import Callable, Sequence

import structlog

from beauty_agent.core import metrics
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.events.consumer import EventConsumer, EventHandler
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.producer import EventProducer, ensure_topics
from beauty_agent.runtime import create_runtime

logger = structlog.get_logger(__name__)

HANDLERS: dict[str, Callable[[], EventHandler]] = {
    "weekly-stats": WeeklyStatsHandler,
}


async def main_async(name: str) -> None:
    settings = get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    runtime = create_runtime(settings)
    handler = HANDLERS[name]()
    dlq = EventProducer(settings.kafka)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows: остановка по Ctrl+C
            loop.add_signal_handler(sig, stop.set)
    metrics.serve_process_metrics()
    await ensure_topics(settings.kafka)
    await dlq.start()
    consumer = EventConsumer(settings.kafka, runtime.session_factory, handler, dlq=dlq, notifier=runtime.notifier)
    await consumer.start()
    logger.info("consumer_started", group=handler.group, topics=list(handler.topics))
    try:
        await consumer.run(stop)
    finally:
        await consumer.stop()  # отдать партиции группе сразу, а не по таймауту сессии
        await dlq.stop()
        await runtime.close()
        logger.info("consumer_stopped", group=handler.group)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Потребитель доменных событий")
    parser.add_argument("handler", choices=sorted(HANDLERS))
    args = parser.parse_args(argv)
    asyncio.run(main_async(args.handler))
    return 0


if __name__ == "__main__":
    sys.exit(main())
