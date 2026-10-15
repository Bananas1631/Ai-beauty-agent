"""Проверки для оркестратора и мониторинга.

/health — процесс жив (перезапускать ли контейнер); /ready — готов принимать трафик:
БД и Redis отвечают. Зависимости проверяются параллельно, каждая со своим таймаутом.
"""

import asyncio
from collections.abc import Awaitable
from typing import Literal

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from beauty_agent.core.config import Settings

logger = structlog.get_logger(__name__)
router = APIRouter(tags=["health"])

CheckStatus = Literal["ok", "error"]


class HealthOut(BaseModel):
    status: Literal["ok"]


class ReadyOut(BaseModel):
    status: Literal["ready", "unavailable"]
    checks: dict[str, CheckStatus]


@router.get("/health")
async def health() -> HealthOut:
    return HealthOut(status="ok")


async def _ping_postgres(engine: AsyncEngine) -> None:
    async with engine.connect() as connection:
        await connection.execute(text("SELECT 1"))


async def _ping_redis(redis: Redis) -> None:
    await redis.ping()


async def _check(name: str, probe: Awaitable[None], timeout_s: float) -> CheckStatus:
    try:
        await asyncio.wait_for(probe, timeout_s)
    except Exception as exc:  # любая ошибка зависимости значит «не готов»
        logger.warning("dependency_unavailable", dependency=name, error_type=type(exc).__name__)
        return "error"
    return "ok"


@router.get(
    "/ready",
    responses={status.HTTP_503_SERVICE_UNAVAILABLE: {"model": ReadyOut, "description": "Зависимость недоступна"}},
)
async def ready(request: Request, response: Response) -> ReadyOut:
    settings: Settings = request.app.state.settings
    timeout = settings.app.ready_timeout_s
    postgres, redis = await asyncio.gather(
        _check("postgres", _ping_postgres(request.app.state.engine), timeout),
        _check("redis", _ping_redis(request.app.state.redis), timeout),
    )
    checks: dict[str, CheckStatus] = {"postgres": postgres, "redis": redis}
    if all(value == "ok" for value in checks.values()):
        return ReadyOut(status="ready", checks=checks)
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyOut(status="unavailable", checks=checks)
