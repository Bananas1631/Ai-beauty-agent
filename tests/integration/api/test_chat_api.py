"""REST API чата: POST → 202 → опрос → ответ, доставка через WebSocket, история и изоляция салонов."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from httpx_ws import AsyncWebSocketSession, WebSocketDisconnect, aconnect_ws
from httpx_ws.transport import ASGIWebSocketTransport
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.channel_helpers import RecordingQueue

from beauty_agent.adapters.crm.fake import FakeCRM
from beauty_agent.catalog.importer import import_catalog
from beauty_agent.conversation.notifier import ChatEvents
from beauty_agent.db.models import Message, Salon
from beauty_agent.db.repositories.api_keys import ApiKeysRepository
from beauty_agent.domain.enums import CrmKind
from beauty_agent.workers.chat import process_chat_message


async def api_key_for(session: AsyncSession, salon: Salon) -> str:
    salon.settings = {"crm_kind": CrmKind.FAKE.value}
    await session.commit()
    await import_catalog(session, FakeCRM(), slug=salon.slug)
    _, key = await ApiKeysRepository(session).issue(salon.id, "тест")
    await session.commit()
    return key


@pytest.fixture
async def key(session: AsyncSession, salon: Salon) -> str:
    return await api_key_for(session, salon)


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.runtime.queue = recording
    return recording


async def worker(
    app: FastAPI, session_factory: async_sessionmaker[AsyncSession], salon: Salon, message_id: str
) -> None:
    """То, что сделал бы воркер Celery по задаче messages.chat."""
    await process_chat_message(
        session_factory, app.state.conversations, ChatEvents(app.state.redis), salon.id, uuid.UUID(message_id)
    )


async def test_post_poll_answer(
    app: FastAPI,
    client: AsyncClient,
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    key: str,
    queue: RecordingQueue,
) -> None:
    headers = {"X-API-Key": key}

    accepted = await client.post(
        "/api/v1/chat", json={"client_id": "app-user-42", "text": "Здравствуйте! Где вы находитесь?"}, headers=headers
    )
    body = accepted.json()
    queued = await client.get(body["status_url"], headers=headers)
    await worker(app, session_factory, salon, body["message_id"])
    done = await client.get(body["status_url"], headers=headers)

    assert accepted.status_code == 202
    assert body["status"] == "queued"
    assert [str(message_id) for _, message_id in queue.chats] == [body["message_id"]]  # задача поставлена
    assert queued.json()["status"] == "queued"
    answer = done.json()
    assert answer["status"] == "done"
    assert answer["reply"]["text"]
    reply = await session.get(Message, answer["reply"]["message_id"])
    assert reply is not None
    assert str(reply.in_reply_to) == body["message_id"]  # ответ ссылается на вопрос


async def test_repeat_task_does_not_answer_twice(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    key: str,
    queue: RecordingQueue,
) -> None:
    accepted = (
        await client.post("/api/v1/chat", json={"client_id": "u-1", "text": "Привет"}, headers={"X-API-Key": key})
    ).json()

    await worker(app, session_factory, salon, accepted["message_id"])
    first = (await client.get(accepted["status_url"], headers={"X-API-Key": key})).json()
    await worker(app, session_factory, salon, accepted["message_id"])  # Celery повторил задачу
    history = (await client.get("/api/v1/clients/u-1/history", headers={"X-API-Key": key})).json()

    assert first["status"] == "done"
    assert [item["role"] for item in history["items"]] == ["agent", "client"]  # один ответ, не два


async def test_idempotency_key(client: AsyncClient, key: str, queue: RecordingQueue) -> None:
    headers = {"X-API-Key": key, "Idempotency-Key": "order-7f3a2c19"}
    payload = {"client_id": "u-2", "text": "Хочу на маникюр"}

    first = await client.post("/api/v1/chat", json=payload, headers=headers)
    second = await client.post("/api/v1/chat", json=payload, headers=headers)

    assert first.json()["message_id"] == second.json()["message_id"]
    assert len(queue.chats) == 1  # вторая задача не поставлена


async def test_auth_and_validation(
    client: AsyncClient, session: AsyncSession, salon: Salon, key: str, queue: RecordingQueue
) -> None:
    payload = {"client_id": "u-3", "text": "Привет"}
    row, revoked_key = await ApiKeysRepository(session).issue(salon.id, "старый")
    await ApiKeysRepository(session).revoke(salon.id, row.id)
    await session.commit()

    responses = [
        await client.post("/api/v1/chat", json=payload),
        await client.post("/api/v1/chat", json=payload, headers={"X-API-Key": "ba_wrong"}),
        await client.post("/api/v1/chat", json=payload, headers={"X-API-Key": revoked_key}),
        await client.post("/api/v1/chat", json={**payload, "client_id": "bad id!"}, headers={"X-API-Key": key}),
        await client.post("/api/v1/chat", json={**payload, "text": ""}, headers={"X-API-Key": key}),
        await client.post("/api/v1/chat", json=payload, headers={"X-API-Key": key, "Idempotency-Key": "x"}),
    ]

    assert [response.status_code for response in responses] == [401, 401, 401, 422, 422, 422]
    assert queue.chats == []


async def test_salons_are_isolated(
    client: AsyncClient,
    session: AsyncSession,
    salon: Salon,
    other_salon: Salon,
    key: str,
    queue: RecordingQueue,
) -> None:
    other_key = await api_key_for(session, other_salon)
    accepted = (
        await client.post("/api/v1/chat", json={"client_id": "shared-id", "text": "Привет"}, headers={"X-API-Key": key})
    ).json()

    foreign_message = await client.get(accepted["status_url"], headers={"X-API-Key": other_key})
    foreign_history = await client.get("/api/v1/clients/shared-id/history", headers={"X-API-Key": other_key})

    assert foreign_message.status_code == 404  # ключ салона B не видит сообщение салона A
    assert foreign_history.status_code == 404  # тот же client_id у салона B — другой клиент, а его нет


async def test_history_keyset_pagination(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    key: str,
    queue: RecordingQueue,
) -> None:
    headers = {"X-API-Key": key}
    for text in ("Привет", "Где вы находитесь?", "Спасибо"):
        accepted = (await client.post("/api/v1/chat", json={"client_id": "u-4", "text": text}, headers=headers)).json()
        await worker(app, session_factory, salon, accepted["message_id"])

    first = (await client.get("/api/v1/clients/u-4/history?limit=4", headers=headers)).json()
    rest = (
        await client.get(f"/api/v1/clients/u-4/history?limit=4&cursor={first['next_cursor']}", headers=headers)
    ).json()

    assert len(first["items"]) == 4
    assert len(rest["items"]) == 2
    assert rest["next_cursor"] is None
    client_texts = [item["content"] for item in first["items"] + rest["items"] if item["role"] == "client"]
    assert client_texts == ["Спасибо", "Где вы находитесь?", "Привет"]  # новые сверху


@asynccontextmanager
async def ws_connect(app: FastAPI, path: str, key: str) -> AsyncIterator[AsyncWebSocketSession]:
    async with httpx.AsyncClient(transport=ASGIWebSocketTransport(app), base_url="http://test") as http:
        ws: AsyncWebSocketSession
        async with aconnect_ws(f"http://test{path}", http, headers={"x-api-key": key}) as ws:
            yield ws


async def test_reply_is_pushed_over_websocket(
    app: FastAPI,
    client: AsyncClient,
    session_factory: async_sessionmaker[AsyncSession],
    salon: Salon,
    key: str,
    queue: RecordingQueue,
) -> None:
    async with ws_connect(app, "/api/v1/chat/ws?client_id=u-5", key) as ws:
        accepted = (
            await client.post("/api/v1/chat", json={"client_id": "u-5", "text": "Привет"}, headers={"X-API-Key": key})
        ).json()
        await asyncio.sleep(0.1)  # подписка Pub/Sub успела оформиться
        await worker(app, session_factory, salon, accepted["message_id"])
        event: dict[str, Any] = json.loads(await asyncio.wait_for(ws.receive_text(), timeout=5))

    assert event["type"] == "message"
    assert event["message_id"] == accepted["message_id"]
    assert event["text"]


async def test_websocket_requires_key(app: FastAPI, redis: Redis) -> None:
    with pytest.raises(BaseException) as raised:  # noqa: PT011 — транспорт оборачивает отказ в ExceptionGroup
        async with ws_connect(app, "/api/v1/chat/ws?client_id=u-6", "ba_wrong"):
            pass
    errors = raised.value.exceptions if isinstance(raised.value, BaseExceptionGroup) else [raised.value]
    assert any(isinstance(error, WebSocketDisconnect) and error.code == 1008 for error in errors)
