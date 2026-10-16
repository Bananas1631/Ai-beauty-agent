"""Ограничение частоты запросов — счётчики в Redis, общие для всех процессов.

Страница салона открыта всему интернету: без лимита её можно заспамить и сжечь бюджет на LLM.
Вебхуки ограничены по каналу после проверки секрета (сломанный или взломанный бот не забьёт
очередь), а неверные секреты считаются по IP: подбирать секрет перебором бессмысленно.
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
PUBLIC_IP_LIMIT = Limit("public_ip", max_hits=120, window_s=60)  # страницы салонов и публичный API
WEBHOOK_ACCOUNT_LIMIT = Limit("webhook_account", max_hits=600, window_s=60)  # один бот: 10 в секунду
WEBHOOK_FAIL_LIMIT = Limit("webhook_fail_ip", max_hits=20, window_s=600)  # неверный секрет с одного IP
ADMIN_USER_LIMIT = Limit("admin_user", max_hits=300, window_s=60)  # API инбокса на администратора
TOKEN_IP_LIMIT = Limit("token_ip", max_hits=20, window_s=60)  # обновление токенов


class RateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def hit(self, limit: Limit, subject: str) -> bool:
        """Засчитывает событие; False — лимит в текущем окне исчерпан."""
        name = key("rate", limit.name, subject)
        count = int(await self.redis.eval(_HIT, 1, name, str(limit.window_s)))
        return count <= limit.max_hits

    async def exhausted(self, limit: Limit, subject: str) -> bool:
        """Лимит уже исчерпан — без засчитывания нового события (для счётчика неудачных попыток)."""
        current = await self.redis.get(key("rate", limit.name, subject))
        return current is not None and int(current) >= limit.max_hits
