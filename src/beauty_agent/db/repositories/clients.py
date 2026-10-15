"""Клиенты: поиск или создание по ClientIdentity («канал + ID пользователя») и профиль для агента.

Телефон приводится к E.164 и уникален в салоне: если клиент назвал номер, который уже есть
у другой карточки, учётная запись мессенджера привязывается к ней — это тот же человек.
"""

import uuid
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from beauty_agent.agent.context import ClientProfile
from beauty_agent.agent.tools import normalize_phone
from beauty_agent.db.models import Booking, Client, ClientIdentity, Master
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.enums import BookingStatus, Channel


class ClientsRepository(SalonRepository[Client]):
    model = Client

    async def by_identity(self, channel: Channel, external_user_id: str) -> Client | None:
        statement = (
            self._select()
            .join(ClientIdentity, ClientIdentity.client_id == Client.id)
            .where(ClientIdentity.channel == channel, ClientIdentity.external_user_id == external_user_id)
        )
        return await self.session.scalar(statement)

    async def by_phone(self, phone: str) -> Client | None:
        return await self.session.scalar(self._select().filter_by(phone=phone))

    async def find_or_create(self, channel: Channel, external_user_id: str, *, name: str | None = None) -> Client:
        client = await self.by_identity(channel, external_user_id)
        if client is not None:
            return client
        client = self.add(Client(name=name[:200] if name else None))
        await self.session.flush()
        # ON CONFLICT: два параллельных первых сообщения одного человека не создадут две учётные записи
        statement = (
            insert(ClientIdentity)
            .values(salon_id=self.salon_id, client_id=client.id, channel=channel, external_user_id=external_user_id)
            .on_conflict_do_nothing(index_elements=["salon_id", "channel", "external_user_id"])
        )
        await self.session.execute(statement)
        existing = await self.by_identity(channel, external_user_id)
        if existing is not None and existing.id != client.id:
            await self.session.delete(client)  # параллельный запрос успел раньше — лишняя карточка не нужна
            return existing
        return client

    async def attach_phone(self, client: Client, raw_phone: str, channel: Channel, external_user_id: str) -> Client:
        """Сохраняет телефон; если он уже у другой карточки — переносит туда учётную запись мессенджера."""
        phone = normalize_phone(raw_phone)
        owner = await self.by_phone(phone)
        if owner is None or owner.id == client.id:
            client.phone = phone
            return client
        identity = await self.session.scalar(
            select(ClientIdentity).where(
                ClientIdentity.salon_id == self.salon_id,
                ClientIdentity.channel == channel,
                ClientIdentity.external_user_id == external_user_id,
            )
        )
        if identity is not None:
            identity.client_id = owner.id
        return owner

    async def profile(self, client: Client) -> ClientProfile:
        """Имя, число визитов и любимый мастер — по завершённым и подтверждённым записям."""
        statement = (
            select(Master.name)
            .join(Booking, Booking.master_id == Master.id)
            .where(
                Booking.salon_id == self.salon_id,
                Booking.client_id == client.id,
                Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.COMPLETED]),
            )
        )
        masters = list(await self.session.scalars(statement))
        favourite = Counter(masters).most_common(1)[0][0] if masters else None
        return ClientProfile(name=client.name, preferred_master=favourite, visits=len(masters))

    async def count_identities(self, client_id: uuid.UUID) -> int:
        statement = select(func.count()).select_from(ClientIdentity).where(ClientIdentity.client_id == client_id)
        return int((await self.session.execute(statement)).scalar_one())
