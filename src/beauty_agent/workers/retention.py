"""Ночная задача: удалить диалоги старше срока хранения из настроек салона (152-ФЗ, ст. 5 ч. 7).

Срок отсчитывается от последнего сообщения диалога, а не от первого: постоянный клиент не теряет
историю, пока переписывается с салоном. Удаление — пачками по 500 диалогов в отдельных транзакциях:
длинная транзакция держала бы блокировки и раздувала WAL. Сообщения, журнал инструментов,
черновики и строки outbox удаляются каскадом. Отправленные служебные ответы без диалога
(отзыв согласия, ответ на голосовое) хранятся 30 дней.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.db.models import Conversation, OutboundMessage, Salon
from beauty_agent.domain.salon_settings import SalonSettings
from beauty_agent.workers.celery_app import celery, run, runtime

logger = structlog.get_logger(__name__)

BATCH = 500
ORPHAN_OUTBOX_DAYS = 30


async def _purge_salon(session_factory: async_sessionmaker[AsyncSession], salon_id: uuid.UUID, cutoff: datetime) -> int:
    deleted = 0
    while True:
        async with session_factory() as session:
            batch = (
                select(Conversation.id)
                .where(Conversation.salon_id == salon_id, Conversation.last_message_at < cutoff)
                .limit(BATCH)
                .scalar_subquery()
            )
            result = await session.execute(
                delete(Conversation).where(Conversation.id.in_(batch)).returning(Conversation.id)
            )
            count = len(result.all())
            await session.commit()
        deleted += count
        if count < BATCH:
            return deleted


async def purge_expired(
    session_factory: async_sessionmaker[AsyncSession], *, now: datetime | None = None
) -> dict[uuid.UUID, int]:
    """Удаляет просроченные диалоги всех салонов; возвращает число удалённых по салонам."""
    now = now or datetime.now(UTC)
    async with session_factory() as session:
        salons = [(row.id, row.settings) for row in (await session.execute(select(Salon.id, Salon.settings))).all()]
    report: dict[uuid.UUID, int] = {}
    for salon_id, raw_settings in salons:
        days = SalonSettings.model_validate(raw_settings or {}).retention_days
        deleted = await _purge_salon(session_factory, salon_id, now - timedelta(days=days))
        if deleted:
            report[salon_id] = deleted
            logger.info("retention_purged", salon_id=str(salon_id), conversations=deleted, days=days)
    async with session_factory() as session:
        await session.execute(
            delete(OutboundMessage).where(
                OutboundMessage.conversation_id.is_(None),
                OutboundMessage.created_at < now - timedelta(days=ORPHAN_OUTBOX_DAYS),
            )
        )
        await session.commit()
    return report


@celery.task(name="periodic.retention")
def retention_task() -> int:
    report = run(purge_expired(runtime().session_factory))
    return sum(report.values())
