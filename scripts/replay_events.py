"""Перечитать журнал событий: с начала хранения, с момента времени или с offset — новой группой Kafka.

    poetry run python -m scripts.replay_events weekly-stats                                # с начала (7 дней)
    poetry run python -m scripts.replay_events weekly-stats --since 2026-12-20T00:00+03:00
    poetry run python -m scripts.replay_events weekly-stats --offset beauty.bookings:3:1200  # из dlq_source

Зачем: исправили обработчик после событий в DLQ; нужно дозаполнить статистику после простоя;
новый потребитель строит данные за всё время хранения (так 29 декабря заполнится ClickHouse).

Своя группа Kafka (по умолчанию <обработчик>-replay-<время>) — живая группа продолжает читать
со своей позиции. Идемпотентность остаётся за обработчиком: уже применённые события пропускаются,
поэтому перечитывание не удваивает статистику. Скрипт завершается, когда дочитал до конца
партиций на момент старта, или по --timeout.
"""

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime

from aiokafka import TopicPartition

from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.events.consumer import EventConsumer, PollResult
from beauty_agent.events.consumers import HANDLERS
from beauty_agent.events.producer import EventProducer
from beauty_agent.runtime import create_runtime


def parse_offset(raw: str) -> tuple[TopicPartition, int]:
    """beauty.bookings:3:1200 — формат заголовка dlq_source."""
    topic, partition, offset = raw.rsplit(":", 2)
    return TopicPartition(topic, int(partition)), int(offset)


async def replay(consumer: EventConsumer, *, timeout_s: float) -> PollResult:
    total = PollResult()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        total += await consumer.poll_once(timeout_ms=1000)
        if await consumer.caught_up():
            break
    return total


async def run(args: argparse.Namespace) -> PollResult:
    settings = get_settings()
    runtime = create_runtime(settings)
    handler = HANDLERS[args.handler](runtime)
    group = args.group or f"{handler.group}-replay-{datetime.now(UTC):%Y%m%d%H%M%S}"
    dlq = EventProducer(settings.kafka)
    await dlq.start()
    consumer = EventConsumer(
        settings.kafka,
        runtime.session_factory,
        handler,
        dlq=dlq,
        notifier=runtime.notifier,
        subscribe=False,
        kafka_group=group,
    )
    await consumer.start()
    try:
        partitions = await consumer.assign_all()
        offsets = dict(parse_offset(raw) for raw in args.offset) if args.offset else None
        await consumer.seek(partitions, since=args.since, offsets=offsets)
        print(f"Группа {group}: партиций {len(partitions)}, читаю…", flush=True)
        return await replay(consumer, timeout_s=args.timeout)
    finally:
        await consumer.stop()
        await dlq.stop()
        await runtime.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Перечитать журнал событий новой группой потребителей")
    parser.add_argument("handler", choices=sorted(HANDLERS))
    parser.add_argument("--group", help="группа Kafka; по умолчанию <обработчик>-replay-<время>")
    parser.add_argument("--since", type=datetime.fromisoformat, help="с момента времени (ISO 8601 с поясом)")
    parser.add_argument("--offset", action="append", help="топик:партиция:offset, можно несколько")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args(argv)
    if args.since is not None and args.since.tzinfo is None:
        parser.error("--since нужен с часовым поясом, например 2026-12-20T00:00+03:00")
    configure_logging("WARNING", json_logs=False)
    result = asyncio.run(run(args))
    print(f"Обработано: {result.handled}, уже были: {result.duplicates}, в DLQ: {result.dead_lettered}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
