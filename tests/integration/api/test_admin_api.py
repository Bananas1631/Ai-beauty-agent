"""API инбокса под JWT, вебхуки MAX и админ-бота."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import jwt
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import RecordingQueue

from beauty_agent.adapters.channels.admin_bot import ADMIN_BOT_ACCOUNT
from beauty_agent.core.security import Security
from beauty_agent.db.models import Client, Salon, SalonAdmin
from beauty_agent.db.repositories.admins import AdminsRepository
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import AdminRole, Channel, MessageRole

ADMIN_ID = "4242"


async def bound_admin(session: AsyncSession, salon: Salon, user_id: str = ADMIN_ID) -> None:
    await AdminsRepository(session).bind(salon.id, user_id, user_id, "Ольга", AdminRole.OWNER)
    await session.commit()


async def conversation_with_messages(session: AsyncSession, salon: Salon, name: str, messages: int) -> uuid.UUID:
    client = Client(salon_id=salon.id, name=name)
    session.add(client)
    await session.flush()
    repo = ConversationsRepository(session, salon.id)
    conversation = await repo.open_for(client.id, Channel.TELEGRAM)
    for number in range(messages):
        await repo.add_message(conversation.id, MessageRole.CLIENT, f"{name}: сообщение {number}")
    await session.commit()
    return conversation.id


def bearer(app: FastAPI, salon: Salon, user_id: str = ADMIN_ID) -> dict[str, str]:
    security: Security = app.state.security
    return {"Authorization": f"Bearer {security.issue_admin_token(user_id=user_id, salon_id=salon.id)}"}


async def test_inbox_requires_valid_token(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    await bound_admin(session, salon)
    security: Security = app.state.security
    expired = security.issue_admin_token(user_id=ADMIN_ID, salon_id=salon.id, now=datetime.now(UTC) - timedelta(days=2))
    none_alg = jwt.encode({"sub": ADMIN_ID, "salon_id": str(salon.id)}, key="", algorithm="none")

    responses = [
        await client.get("/api/v1/admin/conversations"),
        await client.get("/api/v1/admin/conversations", headers={"Authorization": "Bearer garbage"}),
        await client.get("/api/v1/admin/conversations", headers={"Authorization": f"Bearer {expired}"}),
        await client.get("/api/v1/admin/conversations", headers={"Authorization": f"Bearer {none_alg}"}),
        await client.get("/api/v1/admin/conversations", headers=bearer(app, salon, user_id="not-bound")),
    ]

    assert [response.status_code for response in responses] == [401] * 5
    assert responses[0].headers["WWW-Authenticate"] == "Bearer"


async def test_unbound_admin_loses_access_immediately(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    await bound_admin(session, salon)
    headers = bearer(app, salon)
    assert (await client.get("/api/v1/admin/conversations", headers=headers)).status_code == 200

    await session.execute(update(SalonAdmin).values(is_active=False))
    await session.commit()

    assert (await client.get("/api/v1/admin/conversations", headers=headers)).status_code == 401


async def test_conversations_keyset_pagination(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, other_salon: Salon
) -> None:
    await bound_admin(session, salon)
    ids = [await conversation_with_messages(session, salon, f"Клиент {n}", 1) for n in range(3)]
    await conversation_with_messages(session, other_salon, "Чужой", 1)
    headers = bearer(app, salon)

    first = (await client.get("/api/v1/admin/conversations?limit=2", headers=headers)).json()
    second = (
        await client.get(f"/api/v1/admin/conversations?limit=2&cursor={first['next_cursor']}", headers=headers)
    ).json()

    assert [item["id"] for item in first["items"]] == [str(ids[2]), str(ids[1])]  # новые сверху
    assert [item["id"] for item in second["items"]] == [str(ids[0])]
    assert second["next_cursor"] is None
    assert first["items"][0]["client_name"] == "Клиент 2"
    names = {item["client_name"] for item in first["items"] + second["items"]}
    assert "Чужой" not in names  # другой салон не виден


async def test_messages_pagination_and_isolation(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, other_salon: Salon
) -> None:
    await bound_admin(session, salon)
    own = await conversation_with_messages(session, salon, "Анна", 5)
    foreign = await conversation_with_messages(session, other_salon, "Чужой", 1)
    headers = bearer(app, salon)

    page = (await client.get(f"/api/v1/admin/conversations/{own}/messages?limit=3", headers=headers)).json()
    rest = (
        await client.get(
            f"/api/v1/admin/conversations/{own}/messages?limit=3&cursor={page['next_cursor']}", headers=headers
        )
    ).json()
    stolen = await client.get(f"/api/v1/admin/conversations/{foreign}/messages", headers=headers)
    bad_cursor = await client.get(f"/api/v1/admin/conversations/{own}/messages?cursor=bm90LWpzb24", headers=headers)

    contents = [item["content"] for item in page["items"] + rest["items"]]
    assert contents == [f"Анна: сообщение {n}" for n in (4, 3, 2, 1, 0)]
    assert stolen.status_code == 404
    assert bad_cursor.status_code == 400


async def test_last_message_moves_conversation_up(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    await bound_admin(session, salon)
    older = await conversation_with_messages(session, salon, "Старый", 1)
    await conversation_with_messages(session, salon, "Новый", 1)
    await ConversationsRepository(session, salon.id).add_message(older, MessageRole.CLIENT, "Я снова тут")
    await session.commit()

    page = (await client.get("/api/v1/admin/conversations", headers=bearer(app, salon))).json()

    assert page["items"][0]["id"] == str(older)


# ---------------------------------------------------------------- вебхуки MAX и админ-бота


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.inbound.queue = recording
    app.state.runtime.queue = recording
    return recording


def max_update(mid: str, text: str = "Привет") -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": 1,
        "message": {
            "sender": {"user_id": 40021, "first_name": "Анна", "is_bot": False},
            "recipient": {"chat_id": 9000123, "chat_type": "dialog"},
            "body": {"mid": mid, "text": text},
        },
    }


async def test_max_webhook(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon, queue: RecordingQueue
) -> None:
    security: Security = app.state.security
    account = await ChannelAccountsRepository(session).connect(
        salon.id, Channel.MAX, "max-bot-1", {"token": "t"}, security
    )
    await session.commit()
    url = f"/api/v1/webhooks/max/{account.id}"
    headers = {"X-Max-Bot-Api-Secret": security.webhook_secret("max", account.id)}

    forged = await client.post(url, json=max_update("mid.1"), headers={"X-Max-Bot-Api-Secret": "wrong"})
    first = await client.post(url, json=max_update("mid.1"), headers=headers)
    repeat = await client.post(url, json=max_update("mid.1"), headers=headers)

    assert forged.status_code == 401
    assert (first.status_code, repeat.status_code) == (200, 200)
    assert len(queue.scheduled) == 1  # повтор вебхука отсечён


async def test_admin_bot_webhook(app: FastAPI, client: AsyncClient, queue: RecordingQueue) -> None:
    disabled = await client.post("/api/v1/webhooks/admin-bot", json={"update_id": 1})
    app.state.runtime.admin_bot = SimpleNamespace()  # бот включён
    security: Security = app.state.security
    headers = {"X-Telegram-Bot-Api-Secret-Token": security.webhook_secret("admin_bot", ADMIN_BOT_ACCOUNT)}
    update = {"update_id": 77, "callback_query": {"id": "1", "data": "d:s:x"}}

    forged = await client.post(
        "/api/v1/webhooks/admin-bot", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "x"}
    )
    first = await client.post("/api/v1/webhooks/admin-bot", json=update, headers=headers)
    repeat = await client.post("/api/v1/webhooks/admin-bot", json=update, headers=headers)

    assert disabled.status_code == 404
    assert forged.status_code == 401
    assert (first.status_code, repeat.status_code) == (200, 200)
    assert queue.admin_updates == [update]
