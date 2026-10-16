"""Задача Celery для сообщения из POST /api/v1/chat: ответ агента и событие для WebSocket.

API сохранил сообщение со статусом «в очереди» и сразу ответил 202. Здесь: статус «обрабатывается» →
агент → ответ в БД со ссылкой на вопрос и статус «готово» → событие в Redis Pub/Sub. Сбой —
повтор с растущей паузой; после последней попытки статус «ошибка», задача уходит в DLX.
Повтор после сохранённого ответа ничего не пересчитывает: сервис диалога найдёт готовый ответ.
"""

import uuid
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.conversation.notifier import ChatEvents
from beauty_agent.conversation.service import ConversationService, IncomingMessage
from beauty_agent.db.models import ClientIdentity, Conversation, Message, Salon
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.enums import Channel, MessageStatus
from beauty_agent.workers.celery_app import celery, run, runtime
from beauty_agent.workers.messages import MAX_RETRIES, MessageTask, backoff_s

logger = structlog.get_logger(__name__)


async def process_chat_message(
    session_factory: async_sessionmaker[AsyncSession],
    conversations: ConversationService,
    events: ChatEvents,
    salon_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    final_attempt: bool = False,
) -> MessageStatus | None:
    """None — сообщения уже нет (клиент отозвал согласие и переписка удалена)."""
    async with session_factory() as session:
        row = (
            await session.execute(
                select(Message, Salon, ClientIdentity.external_user_id)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .join(Salon, Salon.id == Message.salon_id)
                .join(
                    ClientIdentity,
                    (ClientIdentity.client_id == Conversation.client_id) & (ClientIdentity.channel == Channel.API),
                )
                .where(Message.salon_id == salon_id, Message.id == message_id)
            )
        ).first()
    if row is None:
        return None
    message, salon, client_id = row[0], row[1], str(row[2])
    try:
        reply = await conversations.handle(
            IncomingMessage(salon, Channel.API, client_id, message.content, stored_message_id=message.id)
        )
    except Exception as exc:
        if final_attempt:
            async with session_factory() as session:
                await ConversationsRepository(session, salon_id).set_message_status(
                    message_id, MessageStatus.FAILED, error=f"Агент не ответил: {type(exc).__name__}"
                )
                await session.commit()
            await events.publish(salon_id, client_id, {"type": "failed", "message_id": str(message_id)})
        raise
    if reply.text:
        await events.publish(
            salon_id,
            client_id,
            {"type": "message", "message_id": str(message_id), "text": reply.text, "handed_off": reply.handed_off},
        )
    return MessageStatus.DONE


@celery.task(bind=True, base=MessageTask, name="messages.chat", max_retries=MAX_RETRIES)
def chat_message_task(self: Any, salon_id: str, message_id: str) -> str | None:
    current = runtime()
    final = self.request.retries >= MAX_RETRIES
    try:
        status = run(
            process_chat_message(
                current.session_factory,
                current.conversations,
                ChatEvents(current.redis),
                uuid.UUID(salon_id),
                uuid.UUID(message_id),
                final_attempt=final,
            )
        )
    except Exception as exc:
        if final:
            raise  # статус «ошибка» уже записан; задача уходит в DLX
        logger.warning("chat_retry", attempt=self.request.retries + 1, error=type(exc).__name__)
        raise self.retry(exc=exc, countdown=backoff_s(self.request.retries)) from exc
    return status.value if status else None
