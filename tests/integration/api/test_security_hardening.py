"""Роли и refresh-токены в API инбокса, лимиты частоты, ротация ключа шифрования, виджет."""

import uuid
from datetime import timedelta

from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import AsyncClient
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import RefreshTokens, Security
from beauty_agent.db.models import ChannelAccount, Salon
from beauty_agent.db.repositories.admins import AdminsRepository
from beauty_agent.db.repositories.channels import ChannelAccountsRepository
from beauty_agent.domain.enums import AdminRole, Channel
from scripts.rotate_keys import rotate_all

OWNER = "1001"
ADMIN = "1002"


async def team(session: AsyncSession, salon: Salon) -> None:
    repo = AdminsRepository(session)
    await repo.bind(salon.id, OWNER, OWNER, "Анна", AdminRole.OWNER)
    await repo.bind(salon.id, ADMIN, ADMIN, "Ольга", AdminRole.ADMIN)
    await session.commit()


def bearer(app: FastAPI, salon: Salon, user_id: str, role: AdminRole = AdminRole.ADMIN) -> dict[str, str]:
    security: Security = app.state.security
    return {"Authorization": f"Bearer {security.issue_admin_token(user_id=user_id, salon_id=salon.id, role=role)}"}


async def test_refresh_endpoint_rotates_and_detects_reuse(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    await team(session, salon)
    security: Security = app.state.security
    first = await RefreshTokens(app.state.redis, ttl=security.refresh_ttl).issue(OWNER, salon.id)

    rotated = await client.post("/api/v1/admin/token/refresh", json={"refresh_token": first})
    reused = await client.post("/api/v1/admin/token/refresh", json={"refresh_token": first})
    after_theft = await client.post(
        "/api/v1/admin/token/refresh", json={"refresh_token": rotated.json()["refresh_token"]}
    )

    assert rotated.status_code == 200
    pair = rotated.json()
    assert pair["expires_in"] == 900
    claims = security.verify_admin_token(pair["access_token"])
    assert (claims.user_id, claims.role) == (OWNER, AdminRole.OWNER)  # роль — из БД
    inbox = await client.get("/api/v1/admin/conversations", headers={"Authorization": f"Bearer {pair['access_token']}"})
    assert inbox.status_code == 200
    assert reused.status_code == 401
    assert after_theft.status_code == 401  # повтор старого токена отозвал всю цепочку


async def test_disabled_admin_cannot_refresh(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    await team(session, salon)
    security: Security = app.state.security
    token = await RefreshTokens(app.state.redis, ttl=security.refresh_ttl).issue(ADMIN, salon.id)
    await AdminsRepository(session).deactivate(salon.id, ADMIN)
    await session.commit()

    response = await client.post("/api/v1/admin/token/refresh", json={"refresh_token": token})

    assert response.status_code == 401


async def test_team_is_owner_only(app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon) -> None:
    await team(session, salon)
    # токен говорит «владелец», но в БД он администратор: права берутся из БД
    forged_role = bearer(app, salon, ADMIN, AdminRole.OWNER)
    owner = bearer(app, salon, OWNER, AdminRole.OWNER)
    admin_headers = bearer(app, salon, ADMIN)

    denied = await client.get("/api/v1/admin/team", headers=forged_role)
    listed = await client.get("/api/v1/admin/team", headers=owner)
    self_removal = await client.delete(f"/api/v1/admin/team/{OWNER}", headers=owner)
    removed = await client.delete(f"/api/v1/admin/team/{ADMIN}", headers=owner)
    after = await client.get("/api/v1/admin/conversations", headers=admin_headers)
    bad_id = await client.delete("/api/v1/admin/team/abc", headers=owner)

    assert denied.status_code == 403
    assert [(m["telegram_user_id"], m["role"]) for m in listed.json()] == [(OWNER, "owner"), (ADMIN, "admin")]
    assert self_removal.status_code == 409
    assert removed.status_code == 204
    assert after.status_code == 401  # отключённый администратор теряет доступ сразу
    assert bad_id.status_code == 422


async def test_webhook_secret_bruteforce_is_blocked(
    app: FastAPI, client: AsyncClient, session: AsyncSession, salon: Salon
) -> None:
    security: Security = app.state.security
    account = await ChannelAccountsRepository(session).connect(salon.id, Channel.MAX, "max-1", {"token": "t"}, security)
    await session.commit()
    url = f"/api/v1/webhooks/max/{account.id}"
    good = {"X-Max-Bot-Api-Secret": security.webhook_secret("max", account.id)}

    attempts = [
        (await client.post(url, json={}, headers={"X-Max-Bot-Api-Secret": f"guess-{n}"})).status_code for n in range(21)
    ]
    blocked = await client.post(url, json={}, headers=good)

    assert attempts[:20] == [401] * 20
    assert attempts[20] == 429
    assert blocked.status_code == 429  # с этого IP — даже с верным секретом, пока не истечёт окно
    assert blocked.headers["Retry-After"] == "600"


async def test_public_pages_are_rate_limited(client: AsyncClient, salon: Salon) -> None:
    statuses = [(await client.get(f"/api/v1/salons/{salon.slug}")).status_code for _ in range(121)]

    assert statuses[:120] == [200] * 120
    assert statuses[120] == 429


async def test_key_rotation_keeps_old_tokens(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession], salon: Salon
) -> None:
    old_key, new_key = Fernet.generate_key(), Fernet.generate_key()

    def security(*keys: bytes) -> Security:
        settings = SecuritySettings(
            fernet_keys=SecretStr(",".join(key.decode() for key in keys)),
            webhook_key=SecretStr("w" * 32),
            jwt_secret=SecretStr("j" * 48),
        )
        return Security(settings, env="prod")

    account = await ChannelAccountsRepository(session).connect(
        salon.id, Channel.TELEGRAM, f"bot-{uuid.uuid4().hex[:6]}", {"token": "123:SECRET"}, security(old_key)
    )
    await session.commit()

    rotated = await rotate_all(session_factory, security(new_key, old_key))  # шаг 2–3: новый ключ первым
    await session.refresh(account)

    assert rotated >= 1
    assert security(new_key).decrypt(account.token_encrypted) == {"token": "123:SECRET"}  # старый ключ не нужен
    stored = await session.get(ChannelAccount, account.id)
    assert stored is not None


async def test_widget_script(client: AsyncClient) -> None:
    response = await client.get("/widget.js")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/javascript")
    assert "max-age=3600" in response.headers["cache-control"]
    assert len(response.content) < 30 * 1024  # бюджет виджета — 30 КБ
    script = response.text
    assert 'attachShadow({ mode: "closed" })' in script
    assert "innerHTML" not in script  # текст — только через textContent
    assert "localStorage" in script


def test_refresh_ttl_default() -> None:
    assert Security(SecuritySettings(), env="test").refresh_ttl == timedelta(days=30)
