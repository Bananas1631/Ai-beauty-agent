"""Обмен с индексатором через события (ADR 014): запрос переиндексации и приём её результата.

- EventReindexQueue — переиндексация сайта не задачей Celery, а событием site.reindex.requested
  в event_outbox: индексатор читает его своей группой, основной сервис не знает, сколько у
  индексатора экземпляров и жив ли он сейчас — событие дождётся его в Kafka.
- KnowledgeSyncHandler (группа knowledge-sync) читает knowledge.updated: кладёт версию базы знаний
  сайта в Redis — ключ кэша ответов агента меняется, старые ответы больше не находятся; и
  завершает задачу переиндексации в JobStore, которую видит GET /api/v1/jobs/{id}.
"""

import uuid
from collections.abc import Sequence

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.jobs import JobStore, ReindexQueue
from beauty_agent.db.models import Salon
from beauty_agent.domain.events import Event, KnowledgeUpdated, SiteReindexRequested, SiteReindexRequestedData
from beauty_agent.events.outbox import record_event
from beauty_agent.rag.remote import SITE_VERSION_TTL_S, site_version_key

logger = structlog.get_logger(__name__)

GROUP = "knowledge-sync"
KNOWLEDGE_TOPIC = "beauty.knowledge"


class NoSiteError(ValueError):
    """У салона не указан сайт — индексировать нечего."""


class EventReindexQueue:
    """reindex_site для JobStore: событие индексатору вместо задачи Celery.

    local — свой обход в теневом режиме: задачей Celery управляет он (job_id у него), а индексатор
    получает тот же запрос без job_id — его результат только сравнивается.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], *, local: ReindexQueue | None = None) -> None:
        self.session_factory = session_factory
        self.local = local

    async def reindex_site(self, salon_id: uuid.UUID, job_id: uuid.UUID) -> None:
        async with self.session_factory() as session:
            salon = await session.get(Salon, salon_id)
            site_url = salon.config.site_url if salon is not None else None
            if site_url is None:
                raise NoSiteError("У салона не указан сайт (настройка site_url)")
            event = SiteReindexRequested(
                salon_id=salon_id,
                data=SiteReindexRequestedData(site_url=site_url, job_id=None if self.local else job_id),
            )
            await record_event(session, event)
            await session.commit()
        if self.local is not None:
            await self.local.reindex_site(salon_id, job_id)


class KnowledgeSyncHandler:
    def __init__(self, redis: Redis, *, group: str = GROUP) -> None:
        self.redis = redis
        self._group = group

    @property
    def group(self) -> str:
        return self._group

    @property
    def topics(self) -> Sequence[str]:
        return (KNOWLEDGE_TOPIC,)

    async def handle(self, session: AsyncSession, event: Event) -> None:
        if not isinstance(event, KnowledgeUpdated):
            return  # в том же топике — запросы переиндексации, их читает индексатор
        data = event.data
        if data.status == "done" and data.version:
            await self.redis.set(site_version_key(event.salon_id), data.version, ex=SITE_VERSION_TTL_S)
        if data.job_id is not None:
            jobs = JobStore(self.redis)
            if data.status == "done":
                await jobs.finish(
                    data.job_id,
                    {
                        "pages_seen": data.pages_seen,
                        "pages_indexed": data.pages_indexed,
                        "pages_unchanged": data.pages_unchanged,
                        "chunks": data.chunks,
                        "indexer": True,
                    },
                )
            else:
                await jobs.fail(data.job_id, data.error or "Индексатор не смог обойти сайт", final=True)
        logger.info("knowledge_updated", salon_id=str(event.salon_id), status=data.status, version=data.version)
