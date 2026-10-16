"""3 потребителя одной группы на 6 партиций: по две на каждого; остановка одного посреди пачки
не теряет событий и не даёт двойного эффекта — оставшиеся дочитывают после ребалансировки."""

import asyncio
import contextlib
import time
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import RecordingNotifier
from tests.integration.events.conftest import CommittedDb
from tests.integration.events.test_consumer import message_received, publish
from tests.integration.events.test_replay import messages_counted

from beauty_agent.core.config import KafkaSettings
from beauty_agent.db.models import ProcessedEvent
from beauty_agent.domain.events import Event
from beauty_agent.events.consumer import EventConsumer
from beauty_agent.events.handlers.reports import WeeklyStatsHandler
from beauty_agent.events.producer import EventProducer

SALONS = 24  # ключей больше, чем партиций: у каждого потребителя будут события
PER_SALON = 3


class StuckHandler(WeeklyStatsHandler):
    """Зависает на первом событии пачки — как процесс, который убьют посреди обработки."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.stuck = asyncio.Event()

    async def handle(self, session: AsyncSession, event: Event) -> None:
        self.stuck.set()
        await asyncio.Event().wait()  # дальше не пойдёт: задачу отменят


async def poll_forever(consumer: EventConsumer) -> None:
    while True:
        await consumer.poll_once(timeout_ms=200)


async def wait_for(condition: object, timeout_s: float = 45) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await condition():  # type: ignore[operator]
            return
        await asyncio.sleep(0.2)
    raise AssertionError("не дождались")


async def test_stopping_one_of_three_consumers_loses_nothing(
    kafka: KafkaSettings, committed: CommittedDb, producer: EventProducer, topic: str
) -> None:
    salons = await committed.salons(SALONS)
    group = f"stats-{uuid.uuid4().hex[:8]}"
    stuck = StuckHandler(group=group, topics=[topic])
    consumers = [
        EventConsumer(kafka, committed.factory, handler, dlq=producer, notifier=RecordingNotifier(), retry_delay_s=0.01)
        for handler in (
            WeeklyStatsHandler(group=group, topics=[topic]),
            WeeklyStatsHandler(group=group, topics=[topic]),
            stuck,
        )
    ]
    for consumer in consumers:
        await consumer.start()
    tasks = [asyncio.create_task(poll_forever(consumer)) for consumer in consumers]
    try:

        async def balanced() -> bool:
            return sorted(len(consumer.consumer.assignment()) for consumer in consumers) == [2, 2, 2]

        await wait_for(balanced)
        distribution = [sorted(p.partition for p in consumer.consumer.assignment()) for consumer in consumers]
        await publish(producer, topic, *(message_received(salon) for salon in salons for _ in range(PER_SALON)))
        await asyncio.wait_for(stuck.stuck.wait(), timeout=30)

        tasks[2].cancel()  # «убили» посреди пачки: offset пачки не закоммичен
        with contextlib.suppress(asyncio.CancelledError):
            await tasks[2]
        await consumers[2].stop()  # ушёл из группы — его партиции отдадут оставшимся

        total = SALONS * PER_SALON

        async def all_counted() -> bool:
            return await messages_counted(committed) >= total

        await wait_for(all_counted)
        async with committed.factory() as session:
            marked = await session.scalar(
                select(func.count()).select_from(ProcessedEvent).where(ProcessedEvent.consumer_group == group)
            )
        remaining = sorted(len(consumer.consumer.assignment()) for consumer in consumers[:2])
    finally:
        for task in tasks[:2]:
            task.cancel()
        for task in tasks[:2]:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for consumer in consumers[:2]:
            await consumer.stop()

    assert sorted(len(partitions) for partitions in distribution) == [2, 2, 2]
    assert sorted(p for partitions in distribution for p in partitions) == list(range(6))
    assert remaining == [3, 3]  # партиции ушедшего поделены между оставшимися
    assert await messages_counted(committed) == total  # ни потерь, ни двойного счёта
    assert marked == total
