"""Отправка ответов в мессенджеры через outbox: каждый ответ уходит клиенту ровно один раз.

1. Сервис диалога записывает ответ в outbound_messages в одной транзакции с сообщением агента.
2. Воркер атомарно занимает строку (pending → sending с блокировкой на 60 секунд) и отправляет.
3. Успех — sent; окончательный отказ мессенджера — failed; сбой сети — строка снова pending,
   а задача повторяется. Воркер, упавший посреди отправки, держит блокировку не дольше 60 секунд.
4. Периодическая досылка (redeliver_stale) подбирает строки, которые никто не отправил:
   напоминания при сбое мессенджера, ответы после исчерпания повторов задачи.
"""

import uuid
from collections.abc import Mapping
from datetime import timedelta
from enum import StrEnum
from typing import Any

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.base import ChannelRejectedError, ChannelSender, Delivery
from beauty_agent.core import metrics
from beauty_agent.core.security import Security
from beauty_agent.db.models import ChannelAccount, OutboundMessage
from beauty_agent.domain.enums import Channel, OutboxStatus

logger = structlog.get_logger(__name__)

LOCK = timedelta(seconds=60)


class DeliveryStatus(StrEnum):
    SENT = "sent"
    ALREADY = "already"  # отправлено раньше или прямо сейчас отправляет другой воркер
    FAILED = "failed"
    MISSING = "missing"


def outbox_key(channel: Channel, account_id: uuid.UUID, chat_id: str, incoming_message_id: str) -> str:
    return f"{channel.value}:{account_id}:{chat_id}:{incoming_message_id}"[:300]


async def enqueue_outbound(
    session: AsyncSession,
    *,
    salon_id: uuid.UUID,
    account_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    channel: Channel,
    chat_id: str,
    route: dict[str, Any],
    text: str,
    dedupe_key: str,
) -> None:
    """Строка outbox; повтор с тем же ключом ничего не меняет (ON CONFLICT DO NOTHING)."""
    statement = (
        insert(OutboundMessage)
        .values(
            salon_id=salon_id,
            channel_account_id=account_id,
            conversation_id=conversation_id,
            channel=channel,
            chat_id=chat_id,
            route=route,
            text=text,
            dedupe_key=dedupe_key,
        )
        .on_conflict_do_nothing(index_elements=["salon_id", "dedupe_key"])
    )
    await session.execute(statement)


async def outbox_exists(session: AsyncSession, salon_id: uuid.UUID, dedupe_key: str) -> bool:
    statement = select(OutboundMessage.id).where(
        OutboundMessage.salon_id == salon_id, OutboundMessage.dedupe_key == dedupe_key
    )
    return await session.scalar(statement) is not None


class Outbox:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        security: Security,
        senders: Mapping[Channel, ChannelSender],
    ) -> None:
        self.session_factory = session_factory
        self.security = security
        self.senders = senders

    async def _claim(self, salon_id: uuid.UUID, dedupe_key: str) -> tuple[OutboundMessage, dict[str, Any]] | None:
        async with self.session_factory() as session:
            statement = (
                update(OutboundMessage)
                .where(
                    OutboundMessage.salon_id == salon_id,
                    OutboundMessage.dedupe_key == dedupe_key,
                    or_(
                        OutboundMessage.status == OutboxStatus.PENDING,
                        and_(OutboundMessage.status == OutboxStatus.SENDING, OutboundMessage.locked_until < func.now()),
                    ),
                )
                .values(
                    status=OutboxStatus.SENDING,
                    locked_until=func.now() + LOCK,
                    attempts=OutboundMessage.attempts + 1,
                )
                .returning(OutboundMessage)
            )
            row = (await session.execute(statement)).scalar_one_or_none()
            if row is None:
                await session.rollback()
                return None
            account = await session.get(ChannelAccount, row.channel_account_id)
            await session.commit()
            credentials = self.security.decrypt(account.token_encrypted if account else None)
            return row, credentials

    async def _finish(self, row_id: uuid.UUID, **values: Any) -> None:
        async with self.session_factory() as session:
            await session.execute(update(OutboundMessage).where(OutboundMessage.id == row_id).values(**values))
            await session.commit()

    async def deliver(self, salon_id: uuid.UUID, dedupe_key: str) -> DeliveryStatus:
        claimed = await self._claim(salon_id, dedupe_key)
        if claimed is None:
            return DeliveryStatus.ALREADY
        row, credentials = claimed
        sender = self.senders.get(row.channel)
        if sender is None:
            await self._finish(row.id, status=OutboxStatus.FAILED, error="канал не поддерживается")
            return DeliveryStatus.FAILED
        try:
            external_id = await sender.send(credentials, Delivery(row.chat_id, row.text, row.route, dedupe_key))
        except ChannelRejectedError as exc:
            metrics.provider_error(row.channel.value, "rejected")
            logger.warning("outbound_rejected", channel=row.channel.value, error=exc.message)
            await self._finish(row.id, status=OutboxStatus.FAILED, error=exc.message[:500], locked_until=None)
            return DeliveryStatus.FAILED
        except Exception:
            metrics.provider_error(row.channel.value, "unavailable")
            # Сеть или лимит мессенджера: вернуть строку в очередь, задачу повторит Celery
            await self._finish(row.id, status=OutboxStatus.PENDING, locked_until=None)
            raise
        await self._finish(
            row.id, status=OutboxStatus.SENT, sent_at=func.now(), external_id=external_id, locked_until=None
        )
        return DeliveryStatus.SENT

    async def redeliver_stale(self, *, older_than: timedelta = timedelta(minutes=1), limit: int = 100) -> int:
        """Дослать строки, которые ждут дольше older_than. Возвращает число отправленных."""
        async with self.session_factory() as session:
            rows = (
                await session.execute(
                    select(OutboundMessage.salon_id, OutboundMessage.dedupe_key)
                    .where(
                        OutboundMessage.created_at < func.now() - older_than,
                        or_(
                            OutboundMessage.status == OutboxStatus.PENDING,
                            and_(
                                OutboundMessage.status == OutboxStatus.SENDING,
                                OutboundMessage.locked_until < func.now(),
                            ),
                        ),
                    )
                    .order_by(OutboundMessage.created_at)
                    .limit(limit)
                )
            ).all()
        sent = 0
        for salon_id, dedupe_key in rows:
            try:
                status = await self.deliver(salon_id, dedupe_key)
            except Exception:  # мессенджер ещё недоступен — попробуем в следующий раз
                logger.warning("outbox_redelivery_failed", salon_id=str(salon_id))
                continue
            sent += status is DeliveryStatus.SENT
        return sent
