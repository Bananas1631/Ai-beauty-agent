"""Ограничение частоты сообщений по сессии и IP — счётчики в Redis.

Страница салона открыта всему интернету: без лимита её можно заспамить и сжечь бюджет на LLM.
Фиксированное окно: INCR и EXPIRE одним Lua-скриптом, чтобы счётчик не остался без срока жизни,
если процесс упадёт между командами.
"""

from dataclasses import dataclass

from redis.asyncio import Redis

from beauty_agent.core.redis import key

_HIT = """
local current = redis.call('incr', KEYS[1])
if current == 1 then
    redis.call('expire', KEYS[1], ARGV[1])
end
return current
"""


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    max_hits: int
    window_s: int


SESSION_LIMIT = Limit("session", max_hits=15, window_s=60)
IP_LIMIT = Limit("ip", max_hits=60, window_s=60)


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def hit(self, limit: Limit, subject: str) -> bool:
        """Засчитывает событие; False — лимит в текущем окне исчерпан."""
        name = key("rate", limit.name, subject)
        count = int(await self.redis.eval(_HIT, 1, name, str(limit.window_s)))
        return count <= limit.max_hits
