"""Дневной лимит расходов на LLM для каждого салона — защита от неожиданного счёта.

Страница салона открыта всему интернету: без лимита её можно заспамить и сжечь бюджет.
При превышении агент вежливо передаёт диалог администратору, а владелец сервиса получает
уведомление — один раз в сутки, а не на каждое сообщение.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

from redis.asyncio import Redis

from beauty_agent.core.redis import key

BUDGET_EXCEEDED_ANSWER = "Сейчас передам ваш вопрос администратору — он ответит в ближайшее время. Спасибо за терпение!"
_TTL_S = 2 * 24 * 3600
# Счётчик — целое число десятитысячных рубля: INCRBY атомарен и не копит ошибки округления float
_UNITS = 10_000


class DailyBudget:
    def __init__(self, redis: Redis, *, clock: Callable[[], datetime] | None = None) -> None:
        self.redis = redis
        self.clock = clock or (lambda: datetime.now(UTC))

    def _key(self, salon_id: str, suffix: str = "spent") -> str:
        return key("budget", salon_id, self.clock().date().isoformat(), suffix)

    async def spent(self, salon_id: str) -> Decimal:
        raw = await self.redis.get(self._key(salon_id))
        return Decimal(int(raw)) / _UNITS if raw else Decimal(0)

    async def exceeded(self, salon_id: str, limit_rub: Decimal) -> bool:
        return await self.spent(salon_id) >= limit_rub

    async def add(self, salon_id: str, cost_rub: Decimal) -> Decimal:
        name = self._key(salon_id)
        total = await self.redis.incrby(name, int((cost_rub * _UNITS).to_integral_value()))
        await self.redis.expire(name, _TTL_S)
        return Decimal(total) / _UNITS

    async def first_alert_today(self, salon_id: str) -> bool:
        """True только для первого превышения за сутки — чтобы не спамить уведомлениями."""
        return bool(await self.redis.set(self._key(salon_id, "alerted"), "1", nx=True, ex=_TTL_S))
