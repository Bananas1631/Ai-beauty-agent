import pytest

from beauty_agent.core.resilience import CircuitBreaker, CircuitOpenError, CircuitState, TokenBucket


def state(breaker: CircuitBreaker) -> CircuitState:
    """Состояние через функцию: mypy не «запоминает» значение свойства между проверками."""
    return breaker.state


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def test_bucket_allows_burst_then_rate() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=5, clock=time.clock, sleep=time.sleep)

    for _ in range(5):
        await bucket.acquire()
    assert time.slept == []  # первые 5 запросов — сразу

    await bucket.acquire()
    assert time.slept == [pytest.approx(0.2)]  # шестой ждёт следующего токена: 1/5 секунды


def test_bucket_refills_over_time() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=2, capacity=2, clock=time.clock)
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == 0
    assert bucket.try_acquire() == pytest.approx(0.5)

    time.now += 1.0
    assert bucket.try_acquire() == 0


def test_bucket_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError, match="положительным"):
        TokenBucket(rate=0)


def test_breaker_opens_after_threshold() -> None:
    time = FakeTime()
    breaker = CircuitBreaker("crm", failure_threshold=3, recovery_s=30, clock=time.clock)

    for _ in range(3):
        breaker.before_call()
        breaker.record_failure()

    assert state(breaker) is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.before_call()


def test_success_resets_failure_counter() -> None:
    breaker = CircuitBreaker("crm", failure_threshold=2, clock=lambda: 0.0)
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()

    assert state(breaker) is CircuitState.CLOSED


def test_half_open_lets_one_probe_through() -> None:
    time = FakeTime()
    breaker = CircuitBreaker("crm", failure_threshold=1, recovery_s=30, clock=time.clock)
    breaker.record_failure()
    time.now = 30

    assert state(breaker) is CircuitState.HALF_OPEN
    breaker.before_call()  # пробный вызов пропущен
    with pytest.raises(CircuitOpenError):
        breaker.before_call()  # второй параллельный — нет

    breaker.record_success()
    assert state(breaker) is CircuitState.CLOSED


def test_failed_probe_opens_again() -> None:
    time = FakeTime()
    breaker = CircuitBreaker("crm", failure_threshold=1, recovery_s=30, clock=time.clock)
    breaker.record_failure()
    time.now = 30
    breaker.before_call()
    breaker.record_failure()

    assert state(breaker) is CircuitState.OPEN
    time.now = 59
    assert state(breaker) is CircuitState.OPEN
    time.now = 60
    assert state(breaker) is CircuitState.HALF_OPEN


async def test_redis_bucket_is_shared_between_instances() -> None:
    from fakeredis import FakeAsyncRedis

    from beauty_agent.core.resilience import RedisTokenBucket

    redis = FakeAsyncRedis(decode_responses=True)
    first = RedisTokenBucket(redis, "ba:test:rps", rate=2)
    second = RedisTokenBucket(redis, "ba:test:rps", rate=2)  # другой процесс, тот же ключ

    assert await first.try_acquire() == 0
    assert await second.try_acquire() == 0
    assert await first.try_acquire() > 0  # третий запрос в ту же секунду ждёт: лимит общий
    await redis.aclose()


async def test_redis_bucket_waits_until_token() -> None:
    from fakeredis import FakeAsyncRedis

    from beauty_agent.core.resilience import RedisTokenBucket

    redis = FakeAsyncRedis(decode_responses=True)
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)
        await redis.hset("ba:test:rps2", "tokens", "1")  # «прошло время»: токен появился

    bucket = RedisTokenBucket(redis, "ba:test:rps2", rate=1, sleep=fake_sleep)
    await bucket.acquire()
    await bucket.acquire()

    assert len(waits) == 1
    with pytest.raises(ValueError, match="положительным"):
        RedisTokenBucket(redis, "x", rate=0)
    await redis.aclose()
