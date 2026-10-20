"""Проверка токенов по JWKS в другом сервисе: кэш ключей, перечитывание при смене ключа, недоступный JWKS."""

import httpx
import pytest
from pydantic import SecretStr

from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.jwks import JwksVerifier, KeysUnavailableError
from beauty_agent.core.security import (
    INDEXER_AUDIENCE,
    InsufficientScopeError,
    InvalidTokenError,
    Security,
    generate_signing_key,
)

URL = "http://main.test/.well-known/jwks.json"
FIRST = generate_signing_key()
SECOND = generate_signing_key()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Main:
    """Основной сервис: текущая конфигурация ключей и счётчик запросов JWKS."""

    def __init__(self, keys: str) -> None:
        self.security = Security(SecuritySettings(signing_keys=SecretStr(keys)), env="test")
        self.requests = 0
        self.down = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if self.down:
            return httpx.Response(503)
        return httpx.Response(200, json=self.security.jwks())

    def token(self, scopes: tuple[str, ...] = ("knowledge:read",)) -> str:
        return self.security.issue_access_token(subject="beauty-agent", scopes=scopes)


@pytest.fixture
def main() -> Main:
    return Main(FIRST)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
async def verifier(main: Main, clock: Clock) -> JwksVerifier:
    http = httpx.AsyncClient(transport=httpx.MockTransport(main.handle))
    return JwksVerifier(URL, http, issuer="beauty-agent", audience=INDEXER_AUDIENCE, clock=clock)


async def test_keys_are_cached(main: Main, verifier: JwksVerifier) -> None:
    for _ in range(5):
        await verifier.verify(main.token(), scope="knowledge:read")

    assert main.requests == 1


async def test_unknown_kid_refreshes_keys_once(main: Main, verifier: JwksVerifier, clock: Clock) -> None:
    await verifier.verify(main.token())
    main.security = Security(SecuritySettings(signing_keys=SecretStr(f"{SECOND},{FIRST}")), env="test")
    clock.now += 60  # прошло больше min_refresh_s

    claims = await verifier.verify(main.token())

    assert claims.subject == "beauty-agent"
    assert main.requests == 2


async def test_refresh_is_rate_limited(main: Main, verifier: JwksVerifier, clock: Clock) -> None:
    await verifier.verify(main.token())
    stranger = Main(SECOND)  # токены с чужим kid сыплются потоком
    clock.now += 60

    for _ in range(10):
        with pytest.raises(InvalidTokenError):
            await verifier.verify(stranger.token())

    assert main.requests == 2  # перечитали один раз, а не десять


async def test_stale_keys_are_used_when_jwks_is_down(main: Main, verifier: JwksVerifier, clock: Clock) -> None:
    await verifier.verify(main.token())
    main.down = True
    clock.now += 3600  # кэш устарел

    assert (await verifier.verify(main.token())).subject == "beauty-agent"


async def test_no_keys_at_all_is_unavailable(main: Main, verifier: JwksVerifier) -> None:
    main.down = True

    with pytest.raises(KeysUnavailableError):
        await verifier.verify(main.token())


async def test_scope_is_checked(main: Main, verifier: JwksVerifier) -> None:
    token = main.security.issue_access_token(subject="x", scopes=("knowledge:read",))

    with pytest.raises(InsufficientScopeError):
        await verifier.verify(token, scope="knowledge:write")
