"""GET /api/v1/admin/search: поиск по истории под JWT администратора, только свой салон."""

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration.api.test_admin_api import bearer, bound_admin
from tests.integration.rag.test_history_search import client_with

from beauty_agent.db.models import Salon
from beauty_agent.rag.embeddings import HashingEmbeddings
from beauty_agent.workers.embeddings import embed_pending


async def test_admin_finds_who_asked(
    app: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    other_salon: Salon,
) -> None:
    await bound_admin(session, salon)
    await bound_admin(session, other_salon, user_id="5151")
    asked = await client_with(session, salon, "Анна", ["Сколько стоит наращивание ногтей?"], consent=True)
    await client_with(session, other_salon, "Чужая", ["Наращивание ногтей сколько стоит?"], consent=True)
    await embed_pending(session_factory, HashingEmbeddings())

    found = await client.get("/api/v1/admin/search", params={"q": "наращивание ногтей"}, headers=bearer(app, salon))
    foreign = await client.get(
        "/api/v1/admin/search", params={"q": "наращивание ногтей"}, headers=bearer(app, other_salon, user_id="5151")
    )
    responses = [
        await client.get("/api/v1/admin/search", params={"q": "наращивание"}),
        await client.get("/api/v1/admin/search", params={"q": "н"}, headers=bearer(app, salon)),
        await client.get(
            "/api/v1/admin/search",
            params={"q": "наращивание", "date_from": "2026-10-01", "date_to": "2026-09-01"},
            headers=bearer(app, salon),
        ),
    ]

    assert found.status_code == 200
    hits = found.json()
    assert [hit["message_id"] for hit in hits] == [str(asked[0])]
    assert hits[0]["client_name"] == "Анна"
    assert [hit["client_name"] for hit in foreign.json()] == ["Чужая"]  # каждый видит только свой салон
    assert [response.status_code for response in responses] == [401, 422, 422]
