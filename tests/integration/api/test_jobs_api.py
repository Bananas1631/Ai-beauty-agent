"""API фоновых задач: POST /salons/{id}/reindex → 202, GET /jobs/{id} — прогресс, только своему салону."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import RecordingQueue
from tests.integration.api.test_admin_api import bearer, bound_admin

from beauty_agent.core.jobs import JobStore
from beauty_agent.db.models import Salon

SITE = "https://lavanda.example/"


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.runtime.queue = recording
    app.state.runtime.reindexer = recording  # KNOWLEDGE_BACKEND=local: переиндексация — задачей в очередь
    return recording


async def with_site(session: AsyncSession, salon: Salon) -> None:
    salon.settings = {**salon.settings, "site_url": SITE}
    await session.commit()
    await bound_admin(session, salon)


async def test_reindex_is_queued_and_progress_is_visible(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, queue: RecordingQueue
) -> None:
    await with_site(session, salon)
    headers = bearer(app, salon)

    accepted = await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=headers)
    job = accepted.json()
    jobs = JobStore(app.state.redis)
    await jobs.start(uuid.UUID(job["id"]))
    await jobs.progress(uuid.UUID(job["id"]), 12, 30)
    running = (await client.get(job["status_url"], headers=headers)).json()

    assert accepted.status_code == 202
    assert accepted.headers["Location"] == job["status_url"]
    assert job["status"] == "queued"
    assert queue.reindexed == [(salon.id, uuid.UUID(job["id"]))]
    assert running["status"] == "running"
    assert running["progress"] == {"done": 12, "total": 30, "percent": 40}
    assert running["attempts"] == 1


async def test_second_request_returns_running_job(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, queue: RecordingQueue
) -> None:
    await with_site(session, salon)
    headers = bearer(app, salon)

    first = (await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=headers)).json()
    second = (await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=headers)).json()

    assert second["id"] == first["id"]
    assert len(queue.reindexed) == 1  # вторая задача не поставлена


async def test_other_salon_and_missing_site(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, other_salon: Salon, queue: RecordingQueue
) -> None:
    await with_site(session, salon)
    await bound_admin(session, other_salon, user_id="5151")
    own = (await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=bearer(app, salon))).json()
    foreign = bearer(app, other_salon, user_id="5151")

    responses = [
        await client.post(f"/api/v1/salons/{salon.id}/reindex"),  # без токена
        await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=foreign),  # чужой салон
        await client.get(own["status_url"], headers=foreign),  # чужая задача
        await client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=bearer(app, salon)),
        await client.post(f"/api/v1/salons/{other_salon.id}/reindex", headers=foreign),  # сайта нет
    ]

    assert [response.status_code for response in responses] == [401, 404, 404, 404, 409]
    assert len(queue.reindexed) == 1


async def test_queue_outage_fails_the_job(
    app: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    salon: Salon,
    queue: RecordingQueue,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await with_site(session, salon)

    async def broken(*_: object) -> None:
        raise ConnectionError("rabbitmq down")

    monkeypatch.setattr(queue, "reindex_site", broken)
    response = await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=bearer(app, salon))
    retry = await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=bearer(app, salon))

    assert response.status_code == 503
    assert retry.status_code == 503  # отметка активной задачи снята — повтор пробует снова, а не висит


def test_site_url_is_validated() -> None:
    from pydantic import ValidationError

    from beauty_agent.domain.salon_settings import SalonSettings

    assert SalonSettings(site_url="https://lavanda.example/uslugi").site_url == "https://lavanda.example/uslugi"
    for bad in ("ftp://lavanda.example", "javascript:alert(1)", "lavanda.example", "https://"):
        with pytest.raises(ValidationError):
            SalonSettings(site_url=bad)
