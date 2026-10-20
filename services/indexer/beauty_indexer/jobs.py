"""Очередь обходов сайтов: событие → строка reindex_jobs → обход → knowledge.updated.

Потребитель Kafka только записывает задачу (миллисекунды) и коммитит offset: обход сайта идёт
минутами, и потребитель, который столько не опрашивает Kafka, был бы исключён из группы — его
партиции ушли бы другому, и обход начался бы заново. Поэтому работа отделена от приёма:

- ReindexRequests — обработчик события site.reindex.requested: строка задачи в той же транзакции,
  что и отметка processed_events (повтор события не создаст вторую задачу);
- JobRunner — берёт задачу SELECT … FOR UPDATE SKIP LOCKED (несколько воркеров не берут одну и ту
  же, а салон, который уже обходится, пропускают), обходит сайт и в одной транзакции завершает
  задачу и пишет knowledge.updated в свой outbox. Воркер упал посреди обхода — задача «running»
  без обновлений дольше stale_job_s берётся снова; готовые страницы пропускаются по хешу.
"""

import asyncio
import contextlib
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from beauty_agent.db.base import uuid7
from beauty_agent.domain.errors import DomainError
from beauty_agent.domain.events import Event, KnowledgeUpdated, KnowledgeUpdatedData, SiteReindexRequested
from beauty_agent.events.outbox import record_event
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_indexer.db import EventOutbox, JobStatus, ProcessedEvent, ReindexJob
from beauty_indexer.ingest import index_site
from beauty_indexer.store import KnowledgeStore

logger = structlog.get_logger(__name__)

KNOWLEDGE_TOPIC = "beauty.knowledge"


async def claim(session: AsyncSession, group: str, event: Event) -> bool:
    """Идемпотентность потребителя — в своей таблице processed_events схемы knowledge."""
    statement = (
        insert(ProcessedEvent)
        .values(id=uuid7(), salon_id=event.salon_id, consumer_group=group, event_id=event.event_id)
        .on_conflict_do_nothing(index_elements=["consumer_group", "event_id"])
        .returning(ProcessedEvent.id)
    )
    return (await session.execute(statement)).scalar_one_or_none() is not None


class ReindexRequests:
    def __init__(self, *, group: str = "indexer", max_pages: int = 30) -> None:
        self._group = group
        self.max_pages = max_pages

    @property
    def group(self) -> str:
        return self._group

    @property
    def topics(self) -> Sequence[str]:
        return (KNOWLEDGE_TOPIC,)

    async def handle(self, session: AsyncSession, event: Event) -> None:
        if not isinstance(event, SiteReindexRequested):
            return  # knowledge.updated в том же топике — наше же событие, его читает основной сервис
        session.add(
            ReindexJob(
                salon_id=event.salon_id,
                site_url=event.data.site_url,
                max_pages=min(event.data.max_pages, self.max_pages),
                job_id=event.data.job_id,
            )
        )


class JobRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: EmbeddingsClient,
        *,
        pause_s: float = 0.5,
        stale_after: timedelta = timedelta(minutes=30),
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.embedder = embedder
        self.pause_s = pause_s
        self.stale_after = stale_after
        self.client = client
        self.clock = clock or (lambda: datetime.now(UTC))

    async def take(self) -> ReindexJob | None:
        """Следующая задача: ждущая или брошенная упавшим воркером; салон, который уже обходят, — пропуск."""
        cutoff = self.clock() - self.stale_after
        busy = aliased(ReindexJob)
        salon_busy = exists().where(
            busy.salon_id == ReindexJob.salon_id,
            busy.id != ReindexJob.id,
            busy.status == JobStatus.RUNNING.value,
            busy.updated_at >= cutoff,
        )
        async with self.session_factory() as session, session.begin():
            job = await session.scalar(
                select(ReindexJob)
                .where(
                    or_(
                        ReindexJob.status == JobStatus.PENDING.value,
                        and_(ReindexJob.status == JobStatus.RUNNING.value, ReindexJob.updated_at < cutoff),
                    ),
                    ~salon_busy,
                )
                .order_by(ReindexJob.created_at)
                .limit(1)
                .with_for_update(skip_locked=True, of=ReindexJob)
            )
            if job is None:
                return None
            job.status = JobStatus.RUNNING.value
            job.attempts += 1
            job.updated_at = self.clock()
        return job

    async def run_one(self) -> uuid.UUID | None:
        """Обойти сайт по одной задаче; ID задачи или None, если очередь пуста."""
        job = await self.take()
        if job is None:
            return None
        error: str | None = None
        try:
            report = await index_site(
                self.session_factory,
                job.salon_id,
                job.site_url,
                self.embedder,
                client=self.client,
                max_pages=job.max_pages,
                pause_s=self.pause_s,
            )
        except Exception as exc:  # сайт недоступен, адрес недопустим, модель эмбеддингов упала
            error = exc.message if isinstance(exc, DomainError) else type(exc).__name__
            logger.warning("reindex_job_failed", salon_id=str(job.salon_id), error=error)
            report = None
        status = JobStatus.DONE if report is not None else JobStatus.FAILED
        async with self.session_factory() as session, session.begin():
            version = await KnowledgeStore(session, job.salon_id).version() if report is not None else None
            await session.execute(
                update(ReindexJob)
                .where(ReindexJob.id == job.id)
                .values(status=status.value, error=error, updated_at=self.clock())
            )
            event = KnowledgeUpdated(
                salon_id=job.salon_id,
                data=KnowledgeUpdatedData(
                    status="done" if report is not None else "failed",
                    version=version,
                    job_id=job.job_id,
                    pages_seen=report.pages_seen if report else 0,
                    pages_indexed=report.pages_indexed if report else 0,
                    pages_unchanged=report.pages_unchanged if report else 0,
                    chunks=report.chunks if report else 0,
                    error=error[:300] if error else None,
                ),
            )
            await record_event(session, event, table=EventOutbox)
        return job.id

    async def run(self, stop: asyncio.Event, *, idle_s: float = 1.0) -> None:
        while not stop.is_set():
            try:
                worked = await self.run_one()
            except Exception as exc:  # БД недоступна: задача останется pending или станет «брошенной»
                logger.warning("job_runner_failed", error=type(exc).__name__)
                worked = None
            if worked is None:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=idle_s)
