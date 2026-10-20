"""OAuth2 client credentials: выдача токена, JWKS и доступ партнёра к REST API по scope."""

import base64
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.channel_helpers import RecordingQueue
from tests.integration.api.test_chat_api import api_key_for

from beauty_agent.core.security import API_AUDIENCE, Security, decode_token, jwk_public_key
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.oauth_clients import OAuthClientsRepository

FORM = {"Content-Type": "application/x-www-form-urlencoded"}


async def partner(session: AsyncSession, salon: Salon | None, scopes: list[str]) -> tuple[str, str]:
    client, secret = await OAuthClientsRepository(session).create(
        name="CRM партнёра", scopes=scopes, salon_id=salon.id if salon else None
    )
    await session.commit()
    return client.client_id, secret


def basic(client_id: str, secret: str) -> dict[str, str]:
    raw = base64.b64encode(f"{client_id}:{secret}".encode()).decode()
    return {**FORM, "Authorization": f"Basic {raw}"}


async def token_for(client: AsyncClient, client_id: str, secret: str, scope: str | None = None) -> str:
    body = "grant_type=client_credentials" + (f"&scope={scope}" if scope else "")
    response = await client.post("/oauth/token", content=body, headers=basic(client_id, secret))
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


@pytest.fixture
def queue(app: FastAPI) -> RecordingQueue:
    recording = RecordingQueue()
    app.state.runtime.queue = recording
    app.state.runtime.reindexer = recording
    return recording


async def test_token_by_basic_auth(app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    client_id, secret = await partner(session, salon, ["chat:write", "history:read"])

    response = await client.post(
        "/oauth/token", content="grant_type=client_credentials", headers=basic(client_id, secret)
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["token_type"] == "Bearer"
    assert body["expires_in"] == 900
    assert body["scope"] == "chat:write history:read"
    security: Security = app.state.security
    claims = security.verify_access_token(body["access_token"], audience=API_AUDIENCE, scope="chat:write")
    assert claims.salon_id == salon.id
    assert claims.subject == client_id


async def test_token_by_form_with_narrower_scope(client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    client_id, secret = await partner(session, salon, ["chat:write", "history:read"])
    body = f"grant_type=client_credentials&client_id={client_id}&client_secret={secret}&scope=history:read"

    response = await client.post("/oauth/token", content=body, headers=FORM)

    assert response.status_code == 200
    assert response.json()["scope"] == "history:read"


@pytest.mark.parametrize(
    ("body", "status", "error"),
    [
        ("grant_type=password&username=a&password=b", 400, "unsupported_grant_type"),
        ("grant_type=client_credentials&scope=billing:read", 400, "invalid_scope"),
        ("grant_type=client_credentials&grant_type=client_credentials", 400, "invalid_request"),
    ],
)
async def test_rfc6749_errors(
    client: AsyncClient, session: AsyncSession, salon: Salon, body: str, status: int, error: str
) -> None:
    client_id, secret = await partner(session, salon, ["chat:write"])

    response = await client.post("/oauth/token", content=body, headers=basic(client_id, secret))

    assert response.status_code == status
    assert response.json()["error"] == error


async def test_wrong_secret_and_unknown_client_look_the_same(
    client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    client_id, _ = await partner(session, salon, ["chat:write"])

    wrong = await client.post(
        "/oauth/token", content="grant_type=client_credentials", headers=basic(client_id, "x" * 43)
    )
    unknown = await client.post(
        "/oauth/token", content="grant_type=client_credentials", headers=basic("bac_unknown", "x" * 43)
    )

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert wrong.headers["www-authenticate"].startswith("Basic")


async def test_json_body_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/oauth/token", json={"grant_type": "client_credentials"})

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_request"


async def test_jwks_verifies_issued_token(client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    client_id, secret = await partner(session, salon, ["chat:write"])
    token = await token_for(client, client_id, secret)

    response = await client.get("/.well-known/jwks.json")
    keys = {key["kid"]: jwk_public_key(key) for key in response.json()["keys"]}

    assert response.headers["cache-control"] == "public, max-age=300"
    assert decode_token(token, keys, issuer="beauty-agent", audience=API_AUDIENCE)["sub"] == client_id


async def test_partner_token_works_with_chat_api(
    client: AsyncClient, session: AsyncSession, salon: Salon, queue: RecordingQueue
) -> None:
    await api_key_for(session, salon)  # каталог салона для агента
    client_id, secret = await partner(session, salon, ["chat:write"])
    headers = {"Authorization": f"Bearer {await token_for(client, client_id, secret)}"}

    accepted = await client.post("/api/v1/chat", json={"client_id": "crm-42", "text": "Привет"}, headers=headers)
    history = await client.get("/api/v1/clients/crm-42/history", headers=headers)

    assert accepted.status_code == 202
    assert history.status_code == 403  # history:read не выдан
    assert "history:read" in history.json()["detail"]


async def test_expired_and_salonless_tokens_are_rejected(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    security: Security = app.state.security
    expired = security.issue_access_token(
        subject="bac_1", scopes=["chat:write"], salon_id=salon.id, now=datetime.now(UTC) - timedelta(hours=1)
    )
    internal = security.issue_access_token(subject="beauty-billing", scopes=["chat:write"])

    expired_response = await client.post(
        "/api/v1/chat", json={"client_id": "c", "text": "x"}, headers={"Authorization": f"Bearer {expired}"}
    )
    internal_response = await client.post(
        "/api/v1/chat", json={"client_id": "c", "text": "x"}, headers={"Authorization": f"Bearer {internal}"}
    )

    assert expired_response.status_code == 401
    assert internal_response.status_code == 403  # токен внутреннего сервиса не привязан к салону


async def test_partner_reindex_with_scope(
    client: AsyncClient, session: AsyncSession, salon: Salon, other_salon: Salon, queue: RecordingQueue
) -> None:
    salon.settings = {**salon.settings, "site_url": "https://lavanda.example/"}
    await session.commit()
    client_id, secret = await partner(session, salon, ["reindex:write"])
    headers = {"Authorization": f"Bearer {await token_for(client, client_id, secret)}"}

    own = await client.post(f"/api/v1/salons/{salon.id}/reindex", headers=headers)
    foreign = await client.post(f"/api/v1/salons/{other_salon.id}/reindex", headers=headers)

    assert own.status_code == 202
    assert foreign.status_code == 404
    assert queue.reindexed == [(salon.id, uuid.UUID(own.json()["id"]))]
