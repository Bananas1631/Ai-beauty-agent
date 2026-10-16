"""Входящие сообщения мессенджеров: приём (вебхук или long polling) и обработка в воркере.

Приём — быстро, меньше 100 мс: отсев повтора по ID обновления (SET NX в Redis), сообщение в пачку
склейки и отложенная задача в очередь. Обработка — в воркере Celery: пачка склейки → сервис диалога →
доставка ответа через outbox (в теневом режиме — черновик в админ-бот). Голосовые, стикеры и файлы
получают вежливый ответ, администратор — уведомление.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.base import ChannelUpdate, UpdateKind
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.conversation.outbox import Outbox, enqueue_outbound, outbox_key
from beauty_agent.conversation.service import ConversationService, IncomingMessage, ReplyRoute
from beauty_agent.conversation.shadow_mode import ShadowMode
from beauty_agent.core.redis import key
from beauty_agent.db.repositories.channels import ChannelAccountsRepository

logger = structlog.get_logger(__name__)

DEDUPE_TTL_S = 24 * 3600
START_TEXT = "Здравствуйте!"
UNSUPPORTED_ANSWER = (
    "Пока я понимаю только текстовые сообщения — напишите, пожалуйста, словами. Голосовое передам администратору."
)


class MessageQueue(Protocol):
    async def schedule(self, account_id: uuid.UUID, chat_id: str, token: str, *, delay_s: float) -> None: ...

    async def submit_admin_update(self, payload: dict[str, Any]) -> None:
        """Обновление админ-бота (нажатие кнопки, ответ администратора) — в воркер."""
        ...

    async def sync_booking(self, salon_id: uuid.UUID, external_id: str) -> None:
        """Запись изменилась в YCLIENTS — сверить нашу копию и напоминания (в воркере)."""
        ...

    async def process_chat(self, salon_id: uuid.UUID, message_id: uuid.UUID) -> None:
        """Сообщение из REST API — ответ агента в воркере (workers/chat.py)."""
        ...


@dataclass
class InboundGate:
    """Приём входящих: вызывается из вебхука и из long polling."""

    redis: Redis
    debouncer: Debouncer
    queue: MessageQueue

    async def accept(self, account_id: uuid.UUID, update: ChannelUpdate) -> bool:
        """False — повтор обновления: мессенджер прислал его ещё раз, обработка уже запланирована."""
        first = await self.redis.set(
            key("webhook", update.channel.value, account_id, update.update_id), "1", nx=True, ex=DEDUPE_TTL_S
        )
        if not first:
            return False
        token = await self.debouncer.push(account_id, update.chat_id, update)
        await self.queue.schedule(account_id, update.chat_id, token, delay_s=self.debouncer.delay_s)
        return True


@dataclass
class InboundProcessor:
    """Обработка пачки сообщений одного чата в воркере."""

    session_factory: async_sessionmaker[AsyncSession]
    debouncer: Debouncer
    conversations: ConversationService
    outbox: Outbox
    notifier: Notifier
    shadow: ShadowMode | None = field(default=None)

    async def dead_letter(self, account_id: uuid.UUID) -> None:
        """Сообщение не обработано после всех повторов: уведомить администраторов салона."""
        async with self.session_factory() as session:
            found = await ChannelAccountsRepository(session).get_with_salon(account_id)
        salon_id = str(found[1].id) if found else "-"
        await self.notifier.notify(
            AdminEvent(AdminEventKind.DEAD_LETTER, salon_id, None, "Сообщение клиента не обработано после повторов")
        )

    async def process(self, account_id: uuid.UUID, chat_id: str, token: str) -> None:
        updates = await self.debouncer.take(account_id, chat_id, token)
        if not updates:
            return  # после этой задачи пришли новые сообщения — ответит более поздняя задача
        async with self.session_factory() as session:
            found = await ChannelAccountsRepository(session).get_with_salon(account_id)
        if found is None:  # канал отключили, пока сообщения ждали в очереди
            await self.debouncer.done(account_id, chat_id, token)
            return
        account, salon = found
        last = updates[-1]
        texts = [
            START_TEXT if item.kind is UpdateKind.START else item.text or ""
            for item in updates
            if item.kind is not UpdateKind.UNSUPPORTED
        ]
        texts = [text for text in texts if text]
        if any(item.kind is UpdateKind.UNSUPPORTED for item in updates):
            await self.notifier.notify(
                AdminEvent(AdminEventKind.UNSUPPORTED_MESSAGE, str(salon.id), None, "Клиент прислал голосовое или файл")
            )
        route = ReplyRoute(account.id, chat_id, dict(last.route))
        if texts:
            reply = await self.conversations.handle(
                IncomingMessage(
                    salon=salon,
                    channel=last.channel,
                    external_user_id=last.user_id,
                    text="\n".join(texts),
                    external_message_id=last.message_id,
                    client_name=last.user_name,
                    reply_to=route,
                )
            )
            delivery_key = reply.outbox_key
            if reply.draft_id is not None and self.shadow is not None:
                await self.shadow.publish(reply.draft_id)
        else:
            delivery_key = outbox_key(last.channel, account.id, chat_id, last.message_id)
            async with self.session_factory() as session:
                await enqueue_outbound(
                    session,
                    salon_id=salon.id,
                    account_id=account.id,
                    conversation_id=None,
                    channel=last.channel,
                    chat_id=chat_id,
                    route=dict(last.route),
                    text=UNSUPPORTED_ANSWER,
                    dedupe_key=delivery_key,
                )
                await session.commit()
        if delivery_key:
            status = await self.outbox.deliver(salon.id, delivery_key)
            logger.info("reply_delivered", channel=last.channel.value, status=status.value, messages=len(updates))
        await self.debouncer.done(account_id, chat_id, token)
