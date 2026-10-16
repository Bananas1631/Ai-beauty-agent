"""Переиндексация сайта как фоновая задача: прогресс по страницам и повтор после сбоя на середине."""

import uuid
from collections.abc import Sequence

import pytest
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.jobs import JobKind, JobStatus, JobStore
from beauty_agent.db.models import KnowledgeChunk, Salon
from beauty_agent.domain.errors import LLMUnavailableError
from beauty_agent.rag.embeddings import Embedded, HashingEmbeddings
from beauty_agent.workers.indexing import NoSiteError, reindex_site
from evals.site_server import site_client, site_host


class FlakyEmbeddings(HashingEmbeddings):
    """Модель эмбеддингов, которая падает на n-м вызове — один раз."""

    def __init__(self, fail_on_call: int) -> None:
        self.fail_on_call = fail_on_call
        self.calls = 0

    async def embed(self, texts: Sequence[str]) -> Embedded:
        self.calls += 1
        if self.calls == self.fail_on_call:
            raise LLMUnavailableError("embeddings timeout")
        return await super().embed(texts)


class RecordingJobs(JobStore):
    def __init__(self, redis: Redis) -> None:
        super().__init__(redis)
        self.steps: list[tuple[int, int]] = []

    async def progress(self, job_id: uuid.UUID, done: int, total: int) -> None:
        self.steps.append((done, total))
        await super().progress(job_id, done, total)


async def with_site(session: AsyncSession, salon: Salon) -> None:
    salon.settings = {**salon.settings, "site_url": f"https://{site_host('lavanda')}/"}
    await session.commit()


async def chunks_of(session: AsyncSession, salon: Salon) -> int:
    statement = select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.salon_id == salon.id)
    return int((await session.execute(statement)).scalar_one())


async def test_progress_is_reported_per_page(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon, redis: Redis
) -> None:
    await with_site(session, salon)
    jobs = RecordingJobs(redis)
    job, _ = await jobs.create(salon.id, JobKind.REINDEX)

    async with site_client("lavanda") as client:
        report = await reindex_site(
            session_factory, HashingEmbeddings(), jobs, salon.id, job.id, client=client, pause_s=0
        )
    finished = await jobs.get(job.id)

    assert [done for done, _ in jobs.steps] == [1, 2, 3, 4, 5]
    assert all(done <= total <= 30 for done, total in jobs.steps)
    assert jobs.steps[-1] == (5, 5)
    assert finished is not None
    assert finished.status is JobStatus.DONE
    assert (finished.done, finished.total, finished.attempts) == (5, 5, 1)
    assert finished.result["pages_indexed"] == report.pages_indexed == 5
    assert await chunks_of(session, salon) == report.chunks


async def test_retry_resumes_after_failed_page(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon, redis: Redis
) -> None:
    await with_site(session, salon)
    jobs = JobStore(redis)
    job, _ = await jobs.create(salon.id, JobKind.REINDEX)
    embedder = FlakyEmbeddings(fail_on_call=3)

    async with site_client("lavanda") as client:
        with pytest.raises(LLMUnavailableError):
            await reindex_site(session_factory, embedder, jobs, salon.id, job.id, client=client, pause_s=0)
        after_failure = await jobs.get(job.id)
        saved_before_retry = await chunks_of(session, salon)
        retry = await reindex_site(session_factory, embedder, jobs, salon.id, job.id, client=client, pause_s=0)
    finished = await jobs.get(job.id)

    assert after_failure is not None
    assert (after_failure.status, after_failure.attempts, after_failure.error) == (
        JobStatus.RETRYING,
        1,
        "LLMUnavailableError",
    )
    assert saved_before_retry > 0  # две страницы до сбоя сохранены своими транзакциями
    assert retry.pages_unchanged == 2  # повтор их не пересчитывает
    assert retry.pages_indexed == 3
    assert finished is not None
    assert (finished.status, finished.attempts, finished.error) == (JobStatus.DONE, 2, None)


async def test_final_failure_and_missing_site(
    session_factory: async_sessionmaker[AsyncSession], salon: Salon, redis: Redis
) -> None:
    jobs = JobStore(redis)
    job, _ = await jobs.create(salon.id, JobKind.REINDEX)

    with pytest.raises(NoSiteError):
        await reindex_site(session_factory, HashingEmbeddings(), jobs, salon.id, job.id)
    failed = await jobs.get(job.id)

    assert failed is not None
    assert failed.status is JobStatus.FAILED  # без сайта повторять бессмысленно — сразу «failed»
    assert failed.error is not None
    assert "site_url" in failed.error
