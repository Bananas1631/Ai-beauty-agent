"""Передача диалога человеку: статус «у администратора», агент молчит до возврата.

- Диалог передаётся инструментом агента handoff_to_admin, guardrail-ом (жалоба, скидка),
  при исчерпании бюджета на LLM или если не настроена ни одна модель.
- Администратор получает уведомление в админ-боте с кнопкой «Вернуть агенту» и отвечает клиенту
  ответом на это уведомление: текст уходит в мессенджер клиента через outbox.
- «Вернуть агенту» снова включает агента; состояние диалога в Redis сбрасывается, чтобы агент
  восстановил историю из БД вместе с ответами администратора.
"""

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.agent.state import StateStore
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.conversation.outbox import DeliveryStatus, Outbox, enqueue_outbound
from beauty_agent.db.models import Conversation
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole

logger = structlog.get_logger(__name__)


async def hand_off(
    session: AsyncSession,
    notifier: Notifier,
    salon_id: uuid.UUID,
    conversation_id: uuid.UUID,
    reason: str,
    *,
    kind: AdminEventKind = AdminEventKind.HANDOFF,
) -> None:
    await ConversationsRepository(session, salon_id).set_status(conversation_id, ConversationStatus.HANDED_OFF)
    await notifier.notify(AdminEvent(kind, str(salon_id), str(conversation_id), reason))


@dataclass
class AdminReply:
    status: DeliveryStatus
    conversation_id: uuid.UUID


class Handoff:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], outbox: Outbox, states: StateStore) -> None:
        self.session_factory = session_factory
        self.outbox = outbox
        self.states = states

    async def return_to_agent(self, salon_id: uuid.UUID, conversation_id: uuid.UUID) -> bool:
        """False — диалог уже у агента (повторное нажатие) или не найден."""
        async with self.session_factory() as session:
            returned = await session.scalar(
                update(Conversation)
                .where(
                    Conversation.salon_id == salon_id,
                    Conversation.id == conversation_id,
                    Conversation.status == ConversationStatus.HANDED_OFF,
                )
                .values(status=ConversationStatus.ACTIVE)
                .returning(Conversation.id)
            )
            await session.commit()
        if returned is None:
            return False
        await self.states.forget(str(salon_id), str(conversation_id))
        logger.info("conversation_returned_to_agent", salon_id=str(salon_id))
        return True

    async def reply(
        self, salon_id: uuid.UUID, conversation_id: uuid.UUID, text: str, *, dedupe_key: str
    ) -> AdminReply | None:
        """Ответ администратора клиенту из админ-бота. None — диалог не в мессенджере или удалён."""
        async with self.session_factory() as session:
            conversations = ConversationsRepository(session, salon_id)
            conversation = await conversations.get(conversation_id)
            route = conversation.reply_route if conversation else None
            if conversation is None or not route:
                return None
            await enqueue_outbound(
                session,
                salon_id=salon_id,
                account_id=uuid.UUID(str(route["account_id"])),
                conversation_id=conversation_id,
                channel=Channel(conversation.channel),
                chat_id=str(route["chat_id"]),
                route=dict(route.get("route") or {}),
                text=text,
                dedupe_key=dedupe_key,
            )
            await conversations.add_message(conversation_id, MessageRole.ADMIN, text, external_id=dedupe_key)
            await session.commit()
        await self.states.forget(str(salon_id), str(conversation_id))
        return AdminReply(await self.outbox.deliver(salon_id, dedupe_key), conversation_id)
