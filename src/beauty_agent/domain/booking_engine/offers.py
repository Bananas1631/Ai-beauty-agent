"""Варианты времени, предложенные в диалоге, — в Redis под номерами 1–3.

Клиент отвечает «давайте второй», модель передаёт номер 2, и движок точно знает, какой слот
имелся в виду. Время из текста модели движок не принимает: подставить своё она не может.
"""

import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis

from beauty_agent.core.redis import key
from beauty_agent.domain.entities import Slot

OFFERS_TTL_S = 30 * 60  # через полчаса окно могли занять — варианты устаревают
MAX_OFFERS = 3


class PartOfDay(StrEnum):
    MORNING = "morning"  # до 12:00
    DAY = "day"  # 12:00–17:00
    EVENING = "evening"  # с 17:00

    @classmethod
    def of(cls, local_moment: datetime) -> "PartOfDay":
        if local_moment.hour < 12:
            return cls.MORNING
        if local_moment.hour < 17:
            return cls.DAY
        return cls.EVENING


@dataclass(frozen=True, slots=True)
class Offer:
    number: int
    slot: Slot
    master_name: str
    service_name: str
    price_text: str

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "master_id": self.slot.master_id,
            "service_id": self.slot.service_id,
            "starts_at": self.slot.starts_at.isoformat(),
            "duration_min": self.slot.duration_min,
            "master_name": self.master_name,
            "service_name": self.service_name,
            "price_text": self.price_text,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Offer":
        slot = Slot(
            master_id=str(data["master_id"]),
            service_id=str(data["service_id"]),
            starts_at=datetime.fromisoformat(data["starts_at"]),
            duration_min=int(data["duration_min"]),
        )
        return cls(int(data["number"]), slot, data["master_name"], data["service_name"], data["price_text"])


def pick_offers(slots: list[Slot], tz_slots: list[datetime], *, preferred_master_id: str | None = None) -> list[int]:
    """Индексы 2–3 вариантов: ближайший, в другое время дня, у предпочитаемого (или другого) мастера.

    tz_slots — те же моменты в часовом поясе салона: «утро» и «вечер» считаются по местному времени.
    Возвращает индексы по возрастанию времени.
    """
    if not slots:
        return []
    chosen = [0]

    def parts_of_chosen() -> set[tuple[object, PartOfDay]]:
        return {(tz_slots[i].date(), PartOfDay.of(tz_slots[i])) for i in chosen}

    # в другое время дня (или в другой день)
    for index in range(1, len(slots)):
        if (tz_slots[index].date(), PartOfDay.of(tz_slots[index])) not in parts_of_chosen():
            chosen.append(index)
            break

    # у предпочитаемого мастера, иначе — у мастера, которого ещё нет среди вариантов
    masters = {slots[i].master_id for i in chosen}
    wanted = [
        index
        for index in range(len(slots))
        if index not in chosen and preferred_master_id is not None and slots[index].master_id == preferred_master_id
    ] or [index for index in range(len(slots)) if index not in chosen and slots[index].master_id not in masters]
    if wanted and len(chosen) < MAX_OFFERS:
        chosen.append(wanted[0])

    # добираем ближайшими, если вариантов меньше трёх, но слоты ещё есть
    for index in range(len(slots)):
        if len(chosen) >= MAX_OFFERS:
            break
        if index not in chosen and slots[index].starts_at not in {slots[i].starts_at for i in chosen}:
            chosen.append(index)
    return sorted(chosen)


class OfferStore:
    def __init__(self, redis: Redis, *, ttl_s: int = OFFERS_TTL_S) -> None:
        self.redis = redis
        self.ttl_s = ttl_s

    @staticmethod
    def _key(salon_id: str, conversation_id: str) -> str:
        return key("offers", salon_id, conversation_id)

    async def save(self, salon_id: str, conversation_id: str, offers: list[Offer]) -> None:
        """Новые варианты заменяют прежние: номер «2» всегда относится к последнему списку."""
        payload = json.dumps([offer.to_json() for offer in offers], ensure_ascii=False)
        await self.redis.set(self._key(salon_id, conversation_id), payload, ex=self.ttl_s)

    async def get(self, salon_id: str, conversation_id: str, number: int) -> Offer | None:
        raw = await self.redis.get(self._key(salon_id, conversation_id))
        if raw is None:
            return None
        for item in json.loads(raw):
            if item["number"] == number:
                return Offer.from_json(item)
        return None

    async def all(self, salon_id: str, conversation_id: str) -> list[Offer]:
        raw = await self.redis.get(self._key(salon_id, conversation_id))
        return [] if raw is None else [Offer.from_json(item) for item in json.loads(raw)]

    async def clear(self, salon_id: str, conversation_id: str) -> None:
        await self.redis.delete(self._key(salon_id, conversation_id))
