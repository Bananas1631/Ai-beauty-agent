"""Клиент индексатора: повтор чтения, circuit breaker, новый токен после 401, ответ не по контракту."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from beauty_agent.adapters.knowledge_client import KnowledgeClient, KnowledgeUnavailableError, ServiceTokens
from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.resilience import CircuitBreaker, CircuitOpenError
from beauty_agent.core.security import INDEXER_AUDIENCE, Security

SALON = uuid.uuid4()
SECURITY = Security(SecuritySettings(), env="test")
OK_BODY = {
    "version": "3:2026-12-26T10:00:00+00:00",
    "fragments": [
        {
            "chunk_id": str(uuid.uuid4()),
            "title": "Цены",
            "url": "https://lavanda.example/prices.html",
            "content": "Shellac — 1500 ₽",
            "score": 0.03,
            "distance": 0.2,
            "text_match": True,
            "new_field": "индексатор добавил поле — клиент не ломается",
        }
    ],
}


class Indexer:
    def __init__(self, responses: list[Callable[[httpx.Request], httpx.Response]]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.responses[min(len(self.requests), len(self.responses)) - 1]
        return answer(request)


def ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=OK_BODY)


def status(code: int) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(code, json={"detail": "x"})


def refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


def client(indexer: Indexer, *, breaker: CircuitBreaker | None = None) -> KnowledgeClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(indexer.handle))
    return KnowledgeClient(
        "http://indexer.test/",
        http,
        ServiceTokens(SECURITY, ("knowledge:read",)),
        breaker=breaker,
        retry_delay_s=0,
    )


async def test_search_sends_service_token_and_reads_tolerantly() -> None:
    indexer = Indexer([ok])

    response = await client(indexer).search(SALON, "Сколько стоит Shellac?")

    assert response.fragments[0].title == "Цены"
    request = indexer.requests[0]
    assert request.url.path == f"/v1/salons/{SALON}/search"
    token = request.headers["Authorization"].removeprefix("Bearer ")
    assert SECURITY.verify_access_token(token, audience=INDEXER_AUDIENCE, scope="knowledge:read").salon_id is None


@pytest.mark.parametrize("first", [status(503), refused])
async def test_one_retry_on_network_error_or_unavailable(first: Callable[[httpx.Request], httpx.Response]) -> None:
    indexer = Indexer([first, ok])

    response = await client(indexer).search(SALON, "цены")

    assert len(indexer.requests) == 2
    assert response.version.startswith("3:")


async def test_client_error_is_not_retried() -> None:
    indexer = Indexer([status(422)])

    with pytest.raises(KnowledgeUnavailableError):
        await client(indexer).search(SALON, "цены")

    assert len(indexer.requests) == 1


async def test_unauthorized_gets_new_token_once() -> None:
    indexer = Indexer([status(401), ok])

    await client(indexer).search(SALON, "цены")

    first, second = (request.headers["Authorization"] for request in indexer.requests)
    assert first != second  # после ротации ключа — токен, подписанный новым ключом


async def test_breaker_opens_after_failures() -> None:
    indexer = Indexer([status(503)])
    breaker = CircuitBreaker("indexer", failure_threshold=2, recovery_s=60)
    knowledge = client(indexer, breaker=breaker)

    for _ in range(2):
        with pytest.raises(KnowledgeUnavailableError):
            await knowledge.search(SALON, "цены")
    sent = len(indexer.requests)
    with pytest.raises(CircuitOpenError):
        await knowledge.version(SALON)

    assert len(indexer.requests) == sent  # разомкнут — запрос даже не отправлялся


async def test_contract_violation_is_unavailable() -> None:
    indexer = Indexer([lambda request: httpx.Response(200, json={"fragments": "не список"})])

    with pytest.raises(KnowledgeUnavailableError, match="контракт"):
        await client(indexer).search(SALON, "цены")


async def test_service_token_is_reused_until_expiry() -> None:
    now = datetime(2026, 12, 26, 10, tzinfo=UTC)
    clock = [now]
    tokens = ServiceTokens(SECURITY, ("knowledge:read",), clock=lambda: clock[0])

    first = await tokens.token()
    clock[0] = now + timedelta(minutes=10)
    same = await tokens.token()
    clock[0] = now + timedelta(minutes=14, seconds=30)  # меньше минуты до истечения
    renewed = await tokens.token()

    assert first == same != renewed
