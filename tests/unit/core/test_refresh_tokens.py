"""Роли в JWT и refresh-токены с ротацией: повтор использованного токена отзывает всю цепочку."""

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from fakeredis import FakeAsyncRedis
from pydantic import SecretStr

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import InvalidTokenError, RefreshTokens, Security
from beauty_agent.domain.enums import AdminRole

SALON = uuid.UUID("01a14169-1127-7108-8cb2-73e7127cdfd0")


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


def test_role_travels_in_token() -> None:
    security = Security(SecuritySettings(jwt_secret=SecretStr("j" * 48)), env="test")

    owner = security.verify_admin_token(security.issue_admin_token(user_id="1", salon_id=SALON, role=AdminRole.OWNER))
    admin = security.verify_admin_token(security.issue_admin_token(user_id="2", salon_id=SALON))

    assert (owner.role, admin.role) == (AdminRole.OWNER, AdminRole.ADMIN)
    assert security.jwt_ttl == timedelta(minutes=15)  # access-токен короткий


async def test_refresh_rotation(redis: FakeAsyncRedis) -> None:
    tokens = RefreshTokens(redis, ttl=timedelta(days=30))
    first = await tokens.issue("42", SALON)

    grant = await tokens.use(first)
    second = await tokens.issue(grant.user_id, grant.salon_id, family=grant.family)

    assert (grant.user_id, grant.salon_id) == ("42", SALON)
    assert second != first
    assert await redis.ttl(f"ba:refresh:{tokens._hash(second)}") > 29 * 86400
    assert all(first not in str(value) for value in await redis.keys("*"))  # в Redis только хеш


async def test_reused_token_revokes_whole_family(redis: FakeAsyncRedis) -> None:
    tokens = RefreshTokens(redis, ttl=timedelta(days=30))
    stolen = await tokens.issue("42", SALON)
    grant = await tokens.use(stolen)  # законный клиент обновился
    current = await tokens.issue("42", SALON, family=grant.family)

    with pytest.raises(InvalidTokenError):
        await tokens.use(stolen)  # вор предъявил старый токен
    with pytest.raises(InvalidTokenError):
        await tokens.use(current)  # и законная цепочка отозвана — нужен новый вход через админ-бот


@pytest.mark.parametrize("token", ["", "short", "x" * 500, "unknown-token-of-normal-length-00000"])
async def test_invalid_refresh_tokens(redis: FakeAsyncRedis, token: str) -> None:
    with pytest.raises(InvalidTokenError):
        await RefreshTokens(redis, ttl=timedelta(days=30)).use(token)
