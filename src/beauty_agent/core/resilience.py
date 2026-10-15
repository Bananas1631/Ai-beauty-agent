"""Защита внешних вызовов: ограничитель частоты (token bucket) и circuit breaker.

Ограничитель держит лимит YCLIENTS — 5 запросов в секунду. Пока процесс один, хватает
счётчика в памяти; с несколькими воркерами Celery (блок 6) лимит переедет в Redis.

Circuit breaker после N ошибок подряд «размыкается» и сразу отказывает, не дожидаясь таймаутов:
агент за миллисекунды узнаёт «CRM недоступна» и переводит запись в «администратор подтвердит».
Через recovery_s пропускается один пробный вызов (half-open): успех замыкает цепь, ошибка — снова размыкает.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from beauty_agent.domain.errors import ProviderUnavailableError

if TYPE_CHECKING:
    from redis.asyncio import Redis

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class RateLimiter(Protocol):
    async def acquire(self) -> None: ...


class TokenBucket:
    """До capacity запросов подряд, дальше — rate запросов в секунду."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate должен быть положительным")
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.capacity
        self._updated = clock()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    def try_acquire(self) -> float:
        """Берёт токен и возвращает 0, если он есть; иначе — сколько секунд ждать следующего."""
        self._refill()
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self.rate

    async def acquire(self) -> None:
        # Под замком: ожидающие получают токены по очереди, а не все разом после паузы
        async with self._lock:
            while (wait := self.try_acquire()) > 0:
                await self._sleep(wait)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(ProviderUnavailableError):
    """Цепь разомкнута после серии ошибок — запрос даже не отправлялся."""


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 5,
        recovery_s: float = 30.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_s = recovery_s
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> CircuitState:
        if self._opened_at is None:
            return CircuitState.CLOSED
        if self._clock() - self._opened_at >= self.recovery_s:
            return CircuitState.HALF_OPEN
        return CircuitState.OPEN

    def before_call(self) -> None:
        state = self.state
        if state is CircuitState.OPEN or (state is CircuitState.HALF_OPEN and self._probe_in_flight):
            raise CircuitOpenError(f"{self.name}: временно недоступна")
        if state is CircuitState.HALF_OPEN:
            self._probe_in_flight = True  # пропускаем ровно один пробный вызов

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self._opened_at is not None:  # пробный вызов не удался — снова ждём recovery_s
            self._opened_at = self._clock()
            return
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = self._clock()


# ---------------------------------------------------------------- общий лимит в Redis

# Ведро токенов на все процессы: время берётся из Redis (TIME), а не из часов процесса, —
# у воркеров на разных машинах часы расходятся. Число возвращается строкой: Redis округлил бы
# дробь из Lua до целого.
_BUCKET_SCRIPT = """
local rate = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local time = redis.call('TIME')
local now = tonumber(time[1]) + tonumber(time[2]) / 1000000
local state = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(state[1]) or capacity
local updated = tonumber(state[2]) or now
tokens = math.min(capacity, tokens + math.max(0, now - updated) * rate)
local wait = 0
if tokens >= 1 then
    tokens = tokens - 1
else
    wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], 60)
return tostring(wait)
"""


class RedisTokenBucket:
    """Ограничитель частоты, общий для API и всех воркеров Celery (лимит YCLIENTS — на IP сервера)."""

    def __init__(
        self,
        redis: "Redis",
        name: str,
        rate: float,
        capacity: float | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate должен быть положительным")
        self.redis = redis
        self.name = name
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._sleep = sleep

    async def try_acquire(self) -> float:
        raw = await self.redis.eval(_BUCKET_SCRIPT, 1, self.name, str(self.rate), str(self.capacity))
        return float(raw)

    async def acquire(self) -> None:
        while (wait := await self.try_acquire()) > 0:
            await self._sleep(min(wait, 1.0))
