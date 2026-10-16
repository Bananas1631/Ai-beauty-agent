"""Согласие в диалоге: текст со ссылкой на политику, запись в журнал, отзыв с удалением данных.

Что удаляется при отзыве согласия:
- все диалоги клиента в салоне — вместе с сообщениями, журналом вызовов инструментов, черновиками
  и строками outbox (каскадом по внешним ключам);
- состояние диалогов в Redis;
- учётные записи клиента в мессенджерах; имя и телефон в карточке стираются.
Остаются: обезличенная карточка (на неё ссылаются журнал согласий — доказательство согласия
и его отзыва — и записи на услуги, нужные салону для учёта визитов) и сами записи без имени и телефона.
Эмбеддингов переписки в системе нет: база знаний строится только из сайта и FAQ салона.
"""

import uuid

import structlog
from redis.asyncio import Redis
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.agent.state import StateStore
from beauty_agent.db.models import Client, ClientIdentity, Conversation, Salon
from beauty_agent.db.repositories.consents import ConsentsRepository
from beauty_agent.domain.consent import ConsentState
from beauty_agent.domain.enums import Channel

logger = structlog.get_logger(__name__)


class PrivacyService:
    def __init__(self, redis: Redis, *, public_url: str) -> None:
        self.states = StateStore(redis)
        self.public_url = public_url.rstrip("/")

    def policy_url(self, salon: Salon) -> str:
        return f"{self.public_url}/privacy/{salon.slug}"

    async def state(self, session: AsyncSession, salon_id: uuid.UUID, client_id: uuid.UUID) -> ConsentState:
        return await ConsentsRepository(session, salon_id).state(client_id)

    async def grant(self, session: AsyncSession, salon_id: uuid.UUID, client_id: uuid.UUID, channel: Channel) -> None:
        await ConsentsRepository(session, salon_id).record(client_id, channel, granted=True)
        logger.info("consent_granted", salon_id=str(salon_id))

    async def withdraw(self, session: AsyncSession, salon_id: uuid.UUID, client_id: uuid.UUID, channel: Channel) -> int:
        """Отзыв: запись в журнал и удаление данных. Возвращает число удалённых диалогов."""
        await ConsentsRepository(session, salon_id).record(client_id, channel, granted=False)
        conversation_ids = list(
            await session.scalars(
                select(Conversation.id).where(Conversation.salon_id == salon_id, Conversation.client_id == client_id)
            )
        )
        await session.execute(
            delete(Conversation).where(Conversation.salon_id == salon_id, Conversation.client_id == client_id)
        )
        await session.execute(
            delete(ClientIdentity).where(ClientIdentity.salon_id == salon_id, ClientIdentity.client_id == client_id)
        )
        await session.execute(
            update(Client).where(Client.salon_id == salon_id, Client.id == client_id).values(name=None, phone=None)
        )
        for conversation_id in conversation_ids:
            await self.states.forget(str(salon_id), str(conversation_id))
        logger.info("consent_withdrawn", salon_id=str(salon_id), conversations=len(conversation_ids))
        return len(conversation_ids)
