"""/health, /ready и формат ошибок на настоящих PostgreSQL и Redis."""

import socket

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.api.errors import PROBLEM_JSON
from beauty_agent.api.main import create_app
from beauty_agent.api.middleware import TRACE_HEADER
from beauty_agent.core.config import PostgresSettings, Settings
from beauty_agent.db.models import Salon


def free_port() -> int:
    """Порт, на котором точно никто не слушает: ОС выдала его и сразу освободила."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_when_dependencies_are_up(client: AsyncClient) -> None:
    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "checks": {"postgres": "ok", "redis": "ok"}}


async def test_ready_returns_503_when_database_is_down(settings: Settings) -> None:
    broken = settings.model_copy(update={"postgres": PostgresSettings(host="127.0.0.1", port=free_port())})
    app = create_app(broken)

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "checks": {"postgres": "error", "redis": "ok"}}


async def test_salon_by_slug(client: AsyncClient, salon: Salon) -> None:
    response = await client.get(f"/api/v1/salons/{salon.slug}")

    assert response.status_code == 200
    assert response.json() == {"slug": salon.slug, "name": "Тестовый салон", "timezone": "Europe/Moscow"}


async def test_unknown_salon_is_problem_404(client: AsyncClient, session: AsyncSession) -> None:
    response = await client.get("/api/v1/salons/no-such-salon")

    assert response.status_code == 404
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["title"] == "Not Found"
    assert body["detail"] == "Салон не найден"
    assert body["instance"] == "/api/v1/salons/no-such-salon"
    assert body["trace_id"] == response.headers[TRACE_HEADER]


async def test_invalid_slug_is_problem_422_without_input(client: AsyncClient) -> None:
    response = await client.get("/api/v1/salons/BAD_SLUG")

    assert response.status_code == 422
    body = response.json()
    assert body["errors"][0]["loc"] == ["path", "slug"]
    assert "input" not in body["errors"][0]  # введённое значение не возвращается: там могут быть ПДн


async def test_unexpected_error_is_500_without_details(app: FastAPI, client: AsyncClient) -> None:
    async def boom() -> None:
        raise RuntimeError("пароль от базы: secret")

    app.add_api_route("/boom", boom)

    response = await client.get("/boom", headers={TRACE_HEADER: "trace-from-proxy-1"})

    assert response.status_code == 500
    body = response.json()
    assert body["detail"] == "Внутренняя ошибка сервера"
    assert "secret" not in response.text
    assert body["trace_id"] == "trace-from-proxy-1"
    assert response.headers[TRACE_HEADER] == "trace-from-proxy-1"


async def test_docs_are_hidden_in_prod(settings: Settings) -> None:
    prod = settings.model_copy(update={"app": settings.app.model_copy(update={"env": "prod"})})
    transport = ASGITransport(app=create_app(prod))
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        assert (await http.get("/docs")).status_code == 404
        assert (await http.get("/openapi.json")).status_code == 404
