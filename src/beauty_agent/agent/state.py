"""Состояние диалога: резюме, последние реплики и данные инструментов — в Redis на 24 часа.

Сессия истекла (клиент вернулся через три дня) — состояние восстанавливается из PostgreSQL:
резюме из conversations, реплики из messages, данные инструментов из tool_calls.
Предложенные варианты времени хранит движок записи (OfferStore) — тоже в Redis.
"""

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from redis.asyncio import Redis

from beauty_agent.core.redis import key

STATE_TTL_S = 24 * 3600
MAX_STORED_TURNS = 8  # больше — старые реплики сворачиваются в резюме
MAX_FACTS = 8

TurnRole = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Turn:
    role: TurnRole
    content: str


@dataclass
class DialogState:
    summary: str | None = None
    turns: list[Turn] = field(default_factory=list)
    facts: list[str] = field(default_factory=list)  # результаты инструментов прошлых сообщений

    def remember(self, user_text: str, answer: str, new_facts: list[str]) -> None:
        self.turns.extend([Turn("user", user_text), Turn("assistant", answer)])
        self.facts = [*self.facts, *new_facts][-MAX_FACTS:]

    def to_json(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "turns": [{"role": turn.role, "content": turn.content} for turn in self.turns],
            "facts": self.facts,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "DialogState":
        return cls(
            summary=data.get("summary"),
            turns=[Turn(item["role"], item["content"]) for item in data.get("turns", [])],
            facts=list(data.get("facts", [])),
        )


Restore = Callable[[], Awaitable[DialogState]]


class StateStore:
    def __init__(self, redis: Redis, *, ttl_s: int = STATE_TTL_S) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    @staticmethod
    def _key(salon_id: str, conversation_id: str) -> str:
        return key("dialog", salon_id, conversation_id)

    async def load(self, salon_id: str, conversation_id: str, restore: Restore | None = None) -> DialogState:
        raw = await self.redis.get(self._key(salon_id, conversation_id))
        if raw is not None:
            return DialogState.from_json(json.loads(raw))
        return await restore() if restore is not None else DialogState()

    async def save(self, salon_id: str, conversation_id: str, state: DialogState) -> None:
        payload = json.dumps(state.to_json(), ensure_ascii=False)
        await self.redis.set(self._key(salon_id, conversation_id), payload, ex=self.ttl_s)

    async def forget(self, salon_id: str, conversation_id: str) -> None:
        """Сбросить состояние: следующий ход восстановит историю из БД (правка администратора, отзыв согласия)."""
        await self.redis.delete(self._key(salon_id, conversation_id))
