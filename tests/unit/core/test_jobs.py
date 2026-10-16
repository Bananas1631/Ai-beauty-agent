"""Хранилище фоновых задач: статусы, прогресс, одна активная задача на салон, изоляция салонов."""

import uuid
from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis

from beauty_agent.core.jobs import JOB_TTL_S, JobKind, JobStatus, JobStore

SALON = uuid.uuid4()
OTHER_SALON = uuid.uuid4()


@pytest.fixture
async def jobs() -> AsyncIterator[JobStore]:
    redis = FakeAsyncRedis(decode_responses=True)
    yield JobStore(redis)
    await redis.aclose()


async def test_lifecycle(jobs: JobStore) -> None:
    job, created = await jobs.create(SALON, JobKind.REINDEX)

    await jobs.start(job.id)
    await jobs.progress(job.id, 3, 10)
    running = await jobs.get(job.id)
    await jobs.finish(job.id, {"pages_indexed": 10, "cost_rub": "0.12"})
    done = await jobs.get(job.id)

    assert created
    assert job.status is JobStatus.QUEUED
    assert running is not None
    assert (running.status, running.done, running.total, running.attempts) == (JobStatus.RUNNING, 3, 10, 1)
    assert done is not None
    assert done.status is JobStatus.DONE
    assert done.result == {"pages_indexed": 10, "cost_rub": "0.12"}
    assert 0 < await jobs.redis.ttl(jobs._key(job.id)) <= JOB_TTL_S


async def test_one_active_job_per_salon(jobs: JobStore) -> None:
    first, _ = await jobs.create(SALON, JobKind.REINDEX)
    again, created_again = await jobs.create(SALON, JobKind.REINDEX)
    other, created_other = await jobs.create(OTHER_SALON, JobKind.REINDEX)
    await jobs.finish(first.id, {})
    after, created_after = await jobs.create(SALON, JobKind.REINDEX)

    assert again.id == first.id
    assert not created_again  # идущая задача не запускается второй раз
    assert created_other  # у другого салона — своя
    assert other.id != first.id
    assert created_after  # прежняя завершилась — можно новую
    assert after.id != first.id


async def test_retry_then_final_failure(jobs: JobStore) -> None:
    job, _ = await jobs.create(SALON, JobKind.REINDEX)

    await jobs.start(job.id)
    await jobs.fail(job.id, "LLMUnavailableError", final=False)
    retrying = await jobs.get(job.id)
    await jobs.start(job.id)
    await jobs.fail(job.id, "LLMUnavailableError" * 50, final=True)
    failed = await jobs.get(job.id)
    replacement, created = await jobs.create(SALON, JobKind.REINDEX)

    assert retrying is not None
    assert (retrying.status, retrying.attempts) == (JobStatus.RETRYING, 1)
    assert failed is not None
    assert (failed.status, failed.attempts) == (JobStatus.FAILED, 2)
    assert failed.error is not None
    assert len(failed.error) == 300
    assert created  # окончательный сбой снял отметку активной задачи
    assert replacement.id != job.id


async def test_job_is_visible_only_to_its_salon(jobs: JobStore) -> None:
    job, _ = await jobs.create(SALON, JobKind.REINDEX)

    assert await jobs.get(job.id, salon_id=SALON) is not None
    assert await jobs.get(job.id, salon_id=OTHER_SALON) is None
    assert await jobs.get(uuid.uuid4()) is None


async def test_expired_job_is_not_resurrected(jobs: JobStore) -> None:
    job, _ = await jobs.create(SALON, JobKind.REINDEX)
    await jobs.redis.delete(jobs._key(job.id))

    await jobs.progress(job.id, 1, 2)

    assert await jobs.redis.exists(jobs._key(job.id)) == 0
