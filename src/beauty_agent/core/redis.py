"""Клиент Redis и приёмы поверх него: кэш, блокировки, ключи идемпотентности.

Сначала ими пользуется движок записи, позже — сессии диалога, лимиты частоты и вебхуки.
Ключи не содержат персональных данных: только ID салона, диалога и хеши.
"""

import asyncio
import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

from redis.asyncio import Redis

from beauty_agent.core.config import RedisSettings
from beauty_agent.domain.errors import ConflictError

PREFIX = "ba:"


def make_redis(settings: RedisSettings) -> Redis:
    """Пул соединений с Redis на всё приложение. Ответы — строки, а не bytes."""
    return Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        password=settings.password.get_secret_value() if settings.password else None,
        decode_responses=True,
        socket_connect_timeout=2.0,  # недоступный Redis не должен подвешивать запросы
        socket_timeout=2.0,
        health_check_interval=30,  # проверить соединение, простоявшее без дела дольше 30 секунд
    )


def key(*parts: object) -> str:
    """ba:slots:<salon>:... — общий префикс отделяет наши ключи от чужих в той же базе Redis."""
    return PREFIX + ":".join(str(part) for part in parts)


# ---------------------------------------------------------------- кэш


class JsonCache:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def get(self, name: str) -> Any | None:
        raw = await self.redis.get(name)
        return None if raw is None else json.loads(raw)

    async def set(self, name: str, value: Any, ttl_s: int) -> None:
        await self.redis.set(name, json.dumps(value, ensure_ascii=False, separators=(",", ":")), ex=ttl_s)

    async def delete(self, *names: str) -> None:
        if names:
            await self.redis.delete(*names)


# ---------------------------------------------------------------- блокировки

# Снять блокировку может только тот, кто её поставил: иначе процесс, проспавший TTL,
# снял бы чужую блокировку. Проверка и удаление — атомарно, одним Lua-скриптом.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


class LockBusyError(ConflictError):
    default_message = "Предыдущее сообщение ещё обрабатывается"


class RedisLock:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    @asynccontextmanager
    async def hold(self, name: str, *, ttl_s: float = 30.0, wait_s: float = 0.0) -> AsyncIterator[None]:
        """Держит блокировку на время блока. Не удалось за wait_s — LockBusyError.

        TTL страхует от процесса, упавшего с блокировкой: через ttl_s она снимется сама.
        """
        token = secrets.token_hex(16)
        deadline = asyncio.get_running_loop().time() + wait_s
        while not await self.redis.set(name, token, nx=True, px=int(ttl_s * 1000)):
            if asyncio.get_running_loop().time() >= deadline:
                raise LockBusyError
            await asyncio.sleep(0.05)
        try:
            yield
        finally:
            await self.redis.eval(_RELEASE, 1, name, token)


# ---------------------------------------------------------------- идемпотентность

IdempotencyState = Literal["started", "in_progress", "done"]


@dataclass(frozen=True, slots=True)
class IdempotencyClaim:
    state: IdempotencyState
    result: dict[str, Any] | None = None


class IdempotencyStore:
    """Ключ операции с состояниями «в процессе» и «готово» (с результатом).

    begin() занимает ключ атомарно (SET NX). Пока операция идёт, повтор получает in_progress;
    после complete() — сохранённый результат. При ошибке ключ снимается release(),
    иначе клиент не смог бы повторить запись, пока ключ не истечёт.
    """

    def __init__(self, redis: Redis, *, ttl_s: int = 600) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    async def begin(self, name: str) -> IdempotencyClaim:
        marker = json.dumps({"state": "in_progress"})
        if await self.redis.set(name, marker, nx=True, ex=self.ttl_s):
            return IdempotencyClaim("started")
        raw = await self.redis.get(name)
        if raw is None:  # ключ истёк между SET и GET — пробуем занять ещё раз
            return await self.begin(name)
        stored = json.loads(raw)
        if stored.get("state") == "done":
            return IdempotencyClaim("done", stored.get("result"))
        return IdempotencyClaim("in_progress")

    async def complete(self, name: str, result: dict[str, Any], *, ttl_s: int | None = None) -> None:
        payload = json.dumps({"state": "done", "result": result}, ensure_ascii=False)
        await self.redis.set(name, payload, ex=ttl_s or self.ttl_s)

    async def release(self, name: str) -> None:
        await self.redis.delete(name)
