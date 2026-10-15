"""Подключённые каналы салонов: бот Telegram, сообщество VK, бот MAX.

Вебхук знает только ID канала из адреса, поэтому поиск идёт по ID канала, а салон берётся
из строки канала. Учётные данные хранятся зашифрованными (core/security.py).
"""

import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.core.security import Security
from beauty_agent.db.models import ChannelAccount, Salon
from beauty_agent.domain.enums import Channel


class ChannelAccountsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_with_salon(self, account_id: uuid.UUID) -> tuple[ChannelAccount, Salon] | None:
        statement = (
            select(ChannelAccount, Salon)
            .join(Salon, Salon.id == ChannelAccount.salon_id)
            .where(ChannelAccount.id == account_id, ChannelAccount.is_active)
        )
        row = (await self.session.execute(statement)).first()
        return (row[0], row[1]) if row else None

    async def active(self, channels: Iterable[Channel]) -> Sequence[tuple[ChannelAccount, Salon]]:
        statement = (
            select(ChannelAccount, Salon)
            .join(Salon, Salon.id == ChannelAccount.salon_id)
            .where(ChannelAccount.channel.in_(list(channels)), ChannelAccount.is_active)
        )
        return [(row[0], row[1]) for row in await self.session.execute(statement)]

    async def connect(
        self,
        salon_id: uuid.UUID,
        channel: Channel,
        external_id: str,
        credentials: dict[str, Any],
        security: Security,
    ) -> ChannelAccount:
        """Подключает канал или обновляет учётные данные уже подключённого (шифрует перед записью)."""
        existing = await self.session.scalar(
            select(ChannelAccount).where(ChannelAccount.channel == channel, ChannelAccount.external_id == external_id)
        )
        if existing is not None and existing.salon_id != salon_id:
            raise ValueError("Этот канал уже подключён к другому салону")
        account = existing or ChannelAccount(salon_id=salon_id, channel=channel, external_id=external_id)
        account.token_encrypted = security.encrypt(credentials)
        account.is_active = True
        self.session.add(account)
        await self.session.flush()
        return account
