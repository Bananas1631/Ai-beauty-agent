from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fakeredis import FakeAsyncRedis
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from beauty_agent.adapters.llm.base import ChatMessage
from beauty_agent.adapters.llm.fake import ScriptedLLM, reply
from beauty_agent.agent.budget import DailyBudget
from beauty_agent.agent.router import LLMRouter, build_router
from beauty_agent.api.security import CSP, SecurityHeadersMiddleware
from beauty_agent.core.rate_limit import Limit, RateLimiter
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError
from beauty_agent.domain.salon_settings import SalonSettings

MESSAGES = [ChatMessage("user", "Привет")]


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


# ---------------------------------------------------------------- роутер LLM


async def test_router_falls_back_to_next_model() -> None:
    primary = ScriptedLLM([LLMUnavailableError("timeout")], model_key="qwen3-235b")
    backup = ScriptedLLM([reply("Здравствуйте!")], model_key="gigachat-2-lite")

    response = await LLMRouter([primary, backup]).complete(MESSAGES)

    assert response.text == "Здравствуйте!"
    assert len(primary.calls) == 1  # ту же модель повторно не вызываем


async def test_router_raises_when_all_down() -> None:
    router = LLMRouter([ScriptedLLM([LLMUnavailableError("a")]), ScriptedLLM([LLMUnavailableError("b")])])

    with pytest.raises(LLMUnavailableError, match="Все модели"):
        await router.complete(MESSAGES)


async def test_bad_response_is_not_a_reason_to_switch() -> None:
    backup = ScriptedLLM([reply("не должен вызываться")])
    router = LLMRouter([ScriptedLLM([LLMBadResponseError("кривой JSON")]), backup])

    with pytest.raises(LLMBadResponseError):
        await router.complete(MESSAGES)
    assert backup.calls == []


def test_build_router_order_and_fallback() -> None:
    qwen, giga = ScriptedLLM(model_key="qwen"), ScriptedLLM(model_key="giga")
    clients = {"qwen": qwen, "giga": giga}

    assert build_router(clients, ["giga", "missing", "qwen"]).clients == [giga, qwen]
    offline = ScriptedLLM(model_key="offline")
    assert build_router({}, ["qwen"], fallback=offline).clients == [offline]
    with pytest.raises(ValueError, match="хотя бы одна"):
        build_router({}, ["qwen"])


# ---------------------------------------------------------------- бюджет и лимиты


async def test_daily_budget(redis: FakeAsyncRedis) -> None:
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    budget = DailyBudget(redis, clock=lambda: now)

    await budget.add("salon-1", Decimal("0.0700"))
    total = await budget.add("salon-1", Decimal("0.0300"))

    assert total == Decimal("0.1")
    assert not await budget.exceeded("salon-1", Decimal("0.2"))
    assert await budget.exceeded("salon-1", Decimal("0.1"))
    assert await budget.first_alert_today("salon-1")
    assert not await budget.first_alert_today("salon-1")  # второе уведомление за день не отправляется
    assert await budget.spent("salon-2") == 0

    tomorrow = DailyBudget(redis, clock=lambda: now + timedelta(days=1))
    assert await tomorrow.spent("salon-1") == 0  # новые сутки — новый лимит


async def test_rate_limiter(redis: FakeAsyncRedis) -> None:
    limiter = RateLimiter(redis)
    limit = Limit("test", max_hits=3, window_s=60)

    results = [await limiter.hit(limit, "session-1") for _ in range(4)]

    assert results == [True, True, True, False]
    assert await limiter.hit(limit, "session-2")  # у другой сессии свой счётчик
    assert 0 < await redis.ttl("ba:rate:test:session-1") <= 60


# ---------------------------------------------------------------- настройки салона


def test_salon_settings_defaults_and_validation() -> None:
    settings = SalonSettings.model_validate({})

    assert settings.prompt_version == "v1"
    assert settings.daily_llm_budget_rub == Decimal(300)
    assert SalonSettings(allowed_origins=["https://Studio.Example/"]).allowed_origins == ["https://studio.example"]
    for bad in ({"allowed_origins": ["http://insecure.example"]}, {"prompt_version": "../v1"}, {"unknown": 1}):
        with pytest.raises(ValidationError):
            SalonSettings.model_validate(bad)


# ---------------------------------------------------------------- заголовки безопасности


async def test_security_headers() -> None:
    app = FastAPI()

    @app.get("/page")
    async def page() -> HTMLResponse:
        return HTMLResponse("<p>ok</p>")

    @app.get("/data")
    async def data() -> JSONResponse:
        return JSONResponse({"ok": True})

    app.add_middleware(SecurityHeadersMiddleware)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        html = await client.get("/page")
        json_response = await client.get("/data")

    assert html.headers["Content-Security-Policy"] == CSP
    assert "Content-Security-Policy" not in json_response.headers
    for response in (html, json_response):
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
