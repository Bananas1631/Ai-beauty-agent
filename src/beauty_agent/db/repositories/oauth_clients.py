"""Клиенты OAuth2: выпуск (секрет показывается один раз), проверка секрета, вебхуки салона."""

import asyncio
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.core.security import (
    SCOPES,
    generate_client_secret,
    hash_client_secret,
    needs_rehash,
    verify_client_secret,
)
from beauty_agent.db.models import OAuthClient

CLIENT_PREFIX = "bac_"
TOUCH_EVERY = timedelta(minutes=5)


class OAuthClientsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        name: str,
        scopes: Iterable[str],
        salon_id: uuid.UUID | None = None,
        webhook_url: str | None = None,
        client_id: str | None = None,
    ) -> tuple[OAuthClient, str]:
        granted = sorted(set(scopes))
        unknown = [scope for scope in granted if scope not in SCOPES]
        if not granted or unknown:
            raise ValueError(f"неизвестные scopes: {unknown or 'пусто'}")
        secret = generate_client_secret()
        client = OAuthClient(
            client_id=client_id or CLIENT_PREFIX + secrets.token_hex(8),
            name=name[:100],
            secret_hash=await asyncio.to_thread(hash_client_secret, secret),  # argon2 — десятки мс CPU
            scopes=granted,
            salon_id=salon_id,
            webhook_url=webhook_url,
        )
        self.session.add(client)
        await self.session.flush()
        return client, secret

    async def authenticate(self, client_id: str, secret: str) -> OAuthClient | None:
        """Клиент по client_id и секрету; None — неизвестен, отключён или секрет неверный.

        argon2 считается в потоке (не останавливает цикл событий) и даже для неизвестного
        client_id: по времени ответа нельзя узнать, существует ли клиент.
        """
        client = (
            await self.session.scalar(select(OAuthClient).where(OAuthClient.client_id == client_id[:64]))
            if client_id
            else None
        )
        stored = client.secret_hash if client is not None and client.is_active else None
        valid = await asyncio.to_thread(verify_client_secret, stored, secret)
        if client is None or not valid:
            return None
        now = datetime.now(UTC)
        values: dict[str, object] = {}
        if client.last_used_at is None or now - client.last_used_at > TOUCH_EVERY:
            values["last_used_at"] = now
        if needs_rehash(client.secret_hash):
            values["secret_hash"] = await asyncio.to_thread(hash_client_secret, secret)
        if values:
            await self.session.execute(update(OAuthClient).where(OAuthClient.id == client.id).values(**values))
            await self.session.commit()
        return client

    async def webhooks(self, salon_id: uuid.UUID) -> list[OAuthClient]:
        """Активные партнёры салона с адресом вебхука."""
        rows = await self.session.scalars(
            select(OAuthClient)
            .where(OAuthClient.salon_id == salon_id, OAuthClient.is_active, OAuthClient.webhook_url.is_not(None))
            .order_by(OAuthClient.client_id)
        )
        return list(rows)

    async def deactivate(self, client_id: str) -> bool:
        statement = (
            update(OAuthClient)
            .where(OAuthClient.client_id == client_id, OAuthClient.is_active)
            .values(is_active=False)
            .returning(OAuthClient.id)
        )
        return await self.session.scalar(statement) is not None
