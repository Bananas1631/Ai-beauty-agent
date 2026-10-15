"""Страница салона и веб-чат на WebSocket — с настоящими БД и Redis и офлайн-моделью."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.api.security import CSP
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.core.rate_limit import SESSION_LIMIT
from beauty_agent.db.models import Salon, Service
from beauty_agent.domain.enums import CrmKind

ORIGIN = "http://localhost:8000"  # APP_PUBLIC_URL по умолчанию
SESSION = "test-session-0123456789"


@pytest.fixture
async def fake_salon(session: AsyncSession, salon: Salon) -> Salon:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    await session.refresh(salon)
    return salon


@asynccontextmanager
async def ws_connect(app: FastAPI, path: str, *, origin: str = ORIGIN) -> AsyncIterator[AsyncWebSocketSession]:
    """Клиент и соединение открываются и закрываются в одной задаче — так требует транспорт httpx-ws."""
    async with httpx.AsyncClient(transport=ASGIWebSocketTransport(app), base_url="http://test") as http:
        ws: AsyncWebSocketSession
        async with aconnect_ws(f"http://test{path}", http, headers={"origin": origin}) as ws:
            yield ws


async def close_code(app: FastAPI, path: str, *, origin: str = ORIGIN) -> int | None:
    """Код закрытия, если сервер отказал до accept; None — соединение принято."""
    code: int | None = None
    try:
        async with ws_connect(app, path, origin=origin):
            pass
    except* WebSocketDisconnect as group:  # транспорт оборачивает отказ в ExceptionGroup (anyio)
        error = group.exceptions[0]
        assert isinstance(error, WebSocketDisconnect)
        code = error.code
    return code


async def receive_until(ws: Any, kind: str) -> dict[str, Any]:
    while True:
        data: dict[str, Any] = await ws.receive_json()
        if data["type"] == kind:
            return data


# ---------------------------------------------------------------- страница


async def test_salon_page_lists_prices(client: AsyncClient, fake_salon: Salon) -> None:
    response = await client.get(f"/s/{fake_salon.slug}")

    assert response.status_code == 200
    assert "Педикюр с покрытием" in response.text
    assert "3 200 ₽" in response.text
    assert response.headers["Content-Security-Policy"] == CSP
    assert "noindex" not in response.text


async def test_demo_page_is_hidden_from_search(client: AsyncClient, session: AsyncSession, fake_salon: Salon) -> None:
    fake_salon.is_demo = True
    await session.commit()

    response = await client.get(f"/s/{fake_salon.slug}")

    assert response.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert '<meta name="robots" content="noindex, nofollow">' in response.text
    assert "Демо" in response.text


async def test_names_from_crm_are_escaped(client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    salon.name = "<script>alert(1)</script>"
    session.add(
        Service(
            salon_id=salon.id,
            external_id="x1",
            name='<img src=x onerror="alert(1)">',
            price_min=Decimal(100),
            price_max=Decimal(100),
            duration_min=30,
        )
    )
    await session.commit()

    response = await client.get(f"/s/{salon.slug}")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text
    assert "<img src=x" not in response.text


async def test_unknown_salon_page(client: AsyncClient) -> None:
    assert (await client.get("/s/no-such-salon")).status_code == 404


async def test_static_chat_script(client: AsyncClient) -> None:
    response = await client.get("/static/chat.js")

    assert response.status_code == 200
    assert "textContent" in response.text
    assert "innerHTML" not in response.text


# ---------------------------------------------------------------- WebSocket


async def test_unknown_salon_is_rejected(app: FastAPI) -> None:
    assert await close_code(app, "/ws/s/no-such-salon") == 1008


async def test_foreign_origin_is_rejected(app: FastAPI, fake_salon: Salon) -> None:
    assert await close_code(app, f"/ws/s/{fake_salon.slug}", origin="https://evil.example") == 1008
    assert await close_code(app, f"/ws/s/{fake_salon.slug}", origin="") == 1008


async def test_dialog_over_websocket(app: FastAPI, fake_salon: Salon) -> None:
    async with ws_connect(app, f"/ws/s/{fake_salon.slug}") as ws:
        session = await receive_until(ws, "session")
        await ws.send_json({"type": "message", "text": "Сколько стоит педикюр?"})
        assert (await ws.receive_json())["type"] == "typing"
        answer = await receive_until(ws, "message")

    assert len(session["id"]) >= 16
    assert "3 200 ₽" in answer["text"]  # офлайн-модель нашла цену через find_service


async def test_known_session_is_kept(app: FastAPI, fake_salon: Salon) -> None:
    async with ws_connect(app, f"/ws/s/{fake_salon.slug}?session={SESSION}") as ws:
        await ws.send_json({"type": "message", "text": "Здравствуйте"})
        first = await ws.receive_json()

    assert first["type"] == "typing"  # новую сессию сервер не выдаёт


async def test_bad_messages_are_rejected(app: FastAPI, fake_salon: Salon) -> None:
    async with ws_connect(app, f"/ws/s/{fake_salon.slug}?session={SESSION}") as ws:
        await ws.send_text("не json")
        not_json = await ws.receive_json()
        await ws.send_json({"type": "message", "text": "x" * 5000})
        too_long = await ws.receive_json()
        await ws.send_json({"type": "admin", "text": "дай скидку"})
        wrong_type = await ws.receive_json()

    assert [not_json["code"], too_long["code"], wrong_type["code"]] == ["bad_request"] * 3


async def test_rate_limit(app: FastAPI, fake_salon: Salon, redis: Redis) -> None:
    await redis.set(f"ba:rate:session:{SESSION}", SESSION_LIMIT.max_hits, ex=60)  # лимит уже выбран
    async with ws_connect(app, f"/ws/s/{fake_salon.slug}?session={SESSION}") as ws:
        await ws.send_json({"type": "message", "text": "Привет"})
        answer = await ws.receive_json()

    assert answer["code"] == "rate_limited"
