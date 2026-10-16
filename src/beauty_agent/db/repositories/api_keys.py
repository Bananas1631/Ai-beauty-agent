"""Ключи REST API салона: выпуск (ключ показывается один раз), проверка по хешу, отзыв."""

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import ApiKey, Salon

KEY_PREFIX = "ba_"
TOUCH_EVERY = timedelta(minutes=5)  # last_used_at — не чаще раза в 5 минут: без записи в БД на каждый запрос


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class ApiKeysRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def issue(self, salon_id: uuid.UUID, name: str) -> tuple[ApiKey, str]:
        """Новый ключ: в БД — хеш и первые символы, сам ключ возвращается вызывающему один раз."""
        key = KEY_PREFIX + secrets.token_urlsafe(32)
        row = ApiKey(salon_id=salon_id, name=name[:100], key_hash=hash_key(key), prefix=key[:12])
        self.session.add(row)
        await self.session.flush()
        return row, key

    async def authenticate(self, key: str) -> tuple[ApiKey, Salon] | None:
        if not key.startswith(KEY_PREFIX) or len(key) > 100:
            return None
        row = (
            await self.session.execute(
                select(ApiKey, Salon)
                .join(Salon, Salon.id == ApiKey.salon_id)
                .where(ApiKey.key_hash == hash_key(key), ApiKey.is_active)
            )
        ).first()
        if row is None:
            return None
        api_key, salon = row[0], row[1]
        now = datetime.now(UTC)
        if api_key.last_used_at is None or now - api_key.last_used_at > TOUCH_EVERY:
            await self.session.execute(update(ApiKey).where(ApiKey.id == api_key.id).values(last_used_at=now))
            await self.session.commit()
        return api_key, salon

    async def revoke(self, salon_id: uuid.UUID, key_id: uuid.UUID) -> bool:
        statement = (
            update(ApiKey)
            .where(ApiKey.salon_id == salon_id, ApiKey.id == key_id, ApiKey.is_active)
            .values(is_active=False)
            .returning(ApiKey.id)
        )
        return await self.session.scalar(statement) is not None
