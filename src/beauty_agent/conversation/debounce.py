"""Склейка сообщений: клиент пишет пачкой — агент отвечает один раз на всё сразу.

«Здравствуйте» / «хочу на маникюр» / «в пятницу» — три вебхука за пару секунд. Каждое сообщение
кладётся в список Redis и получает новый токен «последнего сообщения»; задача с задержкой 2,5 секунды
обрабатывает пачку, только если её токен всё ещё последний. Иначе ответит более поздняя задача.

Забранная пачка переезжает в отдельный ключ и живёт час: если обработка упадёт и задача повторится,
она получит ту же пачку, а не пустой список.
"""

import json
import secrets
import uuid

from redis.asyncio import Redis

from beauty_agent.adapters.channels.base import ChannelUpdate
from beauty_agent.core.redis import key

DELAY_S = 2.5
PENDING_TTL_S = 600
BATCH_TTL_S = 3600

# Атомарно: если токен ещё последний — список становится пачкой этого токена.
# Если пачка этого токена уже забрана (повтор задачи) — вернуть её же. Иначе — nil: ответит другая задача.
_TAKE = """
local current = redis.call('GET', KEYS[2])
if current == ARGV[1] then
    if redis.call('EXISTS', KEYS[1]) == 1 then
        redis.call('RENAME', KEYS[1], KEYS[3])
        redis.call('EXPIRE', KEYS[3], ARGV[2])
    end
    redis.call('DEL', KEYS[2])
end
if redis.call('EXISTS', KEYS[3]) == 1 then
    return redis.call('LRANGE', KEYS[3], 0, -1)
end
return false
"""


class Debouncer:
    def __init__(self, redis: Redis, *, delay_s: float = DELAY_S) -> None:
        self.redis = redis
        self.delay_s = delay_s

    @staticmethod
    def _keys(account_id: uuid.UUID, chat_id: str, token: str = "") -> tuple[str, str, str]:
        base = key("debounce", account_id, chat_id)
        return f"{base}:pending", f"{base}:token", f"{base}:batch:{token}"

    async def push(self, account_id: uuid.UUID, chat_id: str, update: ChannelUpdate) -> str:
        """Добавляет сообщение в пачку; возвращает токен для отложенной задачи."""
        token = secrets.token_hex(8)
        pending, token_key, _ = self._keys(account_id, chat_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(pending, json.dumps(update.to_json(), ensure_ascii=False))
            pipe.expire(pending, PENDING_TTL_S)
            pipe.set(token_key, token, ex=PENDING_TTL_S)
            await pipe.execute()
        return token

    async def take(self, account_id: uuid.UUID, chat_id: str, token: str) -> list[ChannelUpdate] | None:
        """Пачка для обработки или None, если после этого токена пришли новые сообщения."""
        pending, token_key, batch = self._keys(account_id, chat_id, token)
        raw = await self.redis.eval(_TAKE, 3, pending, token_key, batch, token, str(BATCH_TTL_S))
        if not raw:
            return None
        return [ChannelUpdate.from_json(json.loads(item)) for item in raw]

    async def done(self, account_id: uuid.UUID, chat_id: str, token: str) -> None:
        await self.redis.delete(self._keys(account_id, chat_id, token)[2])
