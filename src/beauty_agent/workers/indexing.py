"""Переиндексация сайта салона — фоновая задача Celery с прогрессом (POST /salons/{id}/reindex).

Обход сайта идёт минуты: с паузами между страницами, с таймаутами медленного сайта. В HTTP-запросе
или в генераторе демо это зависание, поэтому — задача в очереди periodic (фоновые задачи не
задерживают ответы клиентам в очереди messages).

- Прогресс пишется в JobStore после каждой страницы: GET /api/v1/jobs/{id} показывает «12 из 30».
- Сбой (сайт или модель эмбеддингов недоступны) — повтор с растущей паузой. Страницы сохраняются
  по одной, поэтому повтор продолжает с упавшей: готовые не изменились и пропускаются бесплатно.
- После последней попытки — статус «failed» с текстом ошибки, задача уходит в DLX.
"""

import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.jobs import JobStore
from beauty_agent.db.models import Salon
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.rag.ingest import MAX_PAGES, PAUSE_S, IngestReport, ingest_site
from beauty_agent.workers.celery_app import celery, run, runtime
from beauty_agent.workers.messages import backoff_s

logger = structlog.get_logger(__name__)

REINDEX_RETRIES = 3
REINDEX_TIME_LIMIT_S = 30 * 60  # 30 страниц с паузами и таймаутами медленного сайта


class NoSiteError(ValueError):
    """У салона не указан сайт: переиндексировать нечего, повторять бессмысленно."""


async def reindex_site(
    session_factory: async_sessionmaker[AsyncSession],
    embedder: EmbeddingsClient,
    jobs: JobStore,
    salon_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = MAX_PAGES,
    pause_s: float = PAUSE_S,
    final_attempt: bool = False,
) -> IngestReport:
    await jobs.start(job_id)

    async def progress(done: int, total: int) -> None:
        await jobs.progress(job_id, done, total)

    try:
        async with session_factory() as session:
            salon = await session.get(Salon, salon_id)
            site_url = salon.config.site_url if salon else None
            if site_url is None:
                raise NoSiteError("У салона не указан сайт (настройка site_url)")
            report = await ingest_site(
                session,
                salon_id,
                site_url,
                embedder,
                client=client,
                max_pages=max_pages,
                pause_s=pause_s,
                on_progress=progress,
            )
    except Exception as exc:
        final = final_attempt or isinstance(exc, NoSiteError)
        await jobs.fail(job_id, str(exc) if isinstance(exc, NoSiteError) else type(exc).__name__, final=final)
        logger.warning("reindex_failed", salon_id=str(salon_id), job_id=str(job_id), error=type(exc).__name__)
        raise
    await jobs.finish(job_id, report.as_dict())
    return report


@celery.task(
    bind=True,
    name="jobs.reindex",
    max_retries=REINDEX_RETRIES,
    time_limit=REINDEX_TIME_LIMIT_S,
    soft_time_limit=REINDEX_TIME_LIMIT_S - 60,
)
def reindex_task(self: Any, salon_id: str, job_id: str) -> int:
    current = runtime()
    final = self.request.retries >= REINDEX_RETRIES
    try:
        report = run(
            reindex_site(
                current.session_factory,
                current.conversations.embedder,
                JobStore(current.redis),
                uuid.UUID(salon_id),
                uuid.UUID(job_id),
                final_attempt=final,
            )
        )
    except NoSiteError:
        return 0  # повтор не поможет; статус «failed» с причиной уже записан
    except Exception as exc:
        if final:
            raise
        raise self.retry(exc=exc, countdown=backoff_s(self.request.retries)) from exc
    return report.pages_indexed
