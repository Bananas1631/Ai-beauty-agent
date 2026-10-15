from collections.abc import AsyncIterator

import pytest
from fakeredis import FakeAsyncRedis

from beauty_agent.core.redis import IdempotencyStore, JsonCache, LockBusyError, RedisLock, key


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


def test_key_has_prefix() -> None:
    assert key("slots", "s1", 3) == "ba:slots:s1:3"


async def test_json_cache(redis: FakeAsyncRedis) -> None:
    cache = JsonCache(redis)
    await cache.set("ba:x", {"цена": [1, 2]}, ttl_s=60)

    assert await cache.get("ba:x") == {"цена": [1, 2]}
    assert 0 < await redis.ttl("ba:x") <= 60
    await cache.delete("ba:x")
    assert await cache.get("ba:x") is None


async def test_lock_is_exclusive(redis: FakeAsyncRedis) -> None:
    lock = RedisLock(redis)

    async with lock.hold("ba:lock:chat-1"):
        with pytest.raises(LockBusyError):
            async with lock.hold("ba:lock:chat-1"):
                pass
    async with lock.hold("ba:lock:chat-1"):  # после выхода блокировка свободна
        pass


async def test_lock_does_not_release_foreign_lock(redis: FakeAsyncRedis) -> None:
    lock = RedisLock(redis)

    async with lock.hold("ba:lock:chat-1", ttl_s=30):
        await redis.set("ba:lock:chat-1", "someone-else")  # наш TTL истёк, блокировку взял другой
    assert await redis.get("ba:lock:chat-1") == "someone-else"


async def test_idempotency_states(redis: FakeAsyncRedis) -> None:
    store = IdempotencyStore(redis, ttl_s=600)

    assert (await store.begin("ba:idem:1")).state == "started"
    assert (await store.begin("ba:idem:1")).state == "in_progress"
    await store.complete("ba:idem:1", {"id": "900001"})
    claim = await store.begin("ba:idem:1")
    assert claim.state == "done"
    assert claim.result == {"id": "900001"}


async def test_idempotency_release_allows_retry(redis: FakeAsyncRedis) -> None:
    store = IdempotencyStore(redis)
    await store.begin("ba:idem:2")
    await store.release("ba:idem:2")

    assert (await store.begin("ba:idem:2")).state == "started"
