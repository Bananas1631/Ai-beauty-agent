"""Готовый ответ — событием в Redis Pub/Sub: клиент, подключённый по WebSocket, получает его без опроса.

Канал — на клиента салона: ba:chat:<salon_id>:<client_id>. Pub/Sub не хранит сообщения: если в момент
ответа никто не подписан, событие пропадает — поэтому ответ всегда сохраняется в БД и доступен
через GET /api/v1/chat/messages/{id}. WebSocket — ускорение, а не единственный путь.
"""

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from redis.asyncio import Redis
from redis.asyncio.client import PubSub

from beauty_agent.core.redis import key


def channel_name(salon_id: uuid.UUID, client_id: str) -> str:
    return key("chat", salon_id, client_id)


class ChatEvents:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def publish(self, salon_id: uuid.UUID, client_id: str, event: dict[str, Any]) -> int:
        """Возвращает число подписчиков, получивших событие (0 — клиент сейчас не подключён)."""
        receivers: int = await self.redis.publish(
            channel_name(salon_id, client_id), json.dumps(event, ensure_ascii=False)
        )
        return receivers

    @asynccontextmanager
    async def subscribe(self, salon_id: uuid.UUID, client_id: str) -> AsyncIterator[PubSub]:
        pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        await pubsub.subscribe(channel_name(salon_id, client_id))
        try:
            yield pubsub
        finally:
            await pubsub.unsubscribe()
            await pubsub.aclose()  # type: ignore[no-untyped-call]  # у redis-py нет аннотации
