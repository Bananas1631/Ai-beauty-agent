"""Сервис диалога — единая точка входа для веб-чата и мессенджеров.

Принимает сообщение, находит или создаёт клиента по ClientIdentity и диалог, запускает агента
и сохраняет ответ с токенами и стоимостью. Одно сообщение диалога обрабатывается за раз
(блокировка в Redis): второе ждёт первое, иначе агент ответил бы дважды на одно и то же.

Для мессенджеров ответ не отправляется отсюда, а записывается в outbox в той же транзакции,
что и сообщение агента: отправку делает воркер, и повтор задачи не даст второго ответа.
"""

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.agent.budget import BUDGET_EXCEEDED_ANSWER, DailyBudget
from beauty_agent.agent.loop import HANDOFF_ANSWER, Agent, AgentResult
from beauty_agent.agent.registry import ToolContext
from beauty_agent.agent.router import build_router
from beauty_agent.agent.state import StateStore
from beauty_agent.agent.tools import default_registry
from beauty_agent.catalog.lookup import DbCatalog
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.conversation.outbox import enqueue_outbound, outbox_key
from beauty_agent.core.redis import RedisLock, key
from beauty_agent.core.tracing import NoopTracer, Tracer
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.bookings import SqlBookingLedger
from beauty_agent.db.repositories.clients import ClientsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.domain.booking_engine.service import BookingService
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole
from beauty_agent.rag.embeddings import EmbeddingsClient, HashingEmbeddings
from beauty_agent.rag.retriever import HybridRetriever, RagSalonInfo

logger = structlog.get_logger(__name__)

HANDED_OFF_ANSWER = "Ваш вопрос уже у администратора — он ответит в ближайшее рабочее время."
LOCK_TTL_S = 120.0  # дольше самого медленного ответа агента: 6 шагов по 15 секунд с запасом
LOCK_WAIT_S = 60.0


@dataclass(frozen=True, slots=True)
class ReplyRoute:
    """Куда отправить ответ в мессенджер: аккаунт канала салона и чат клиента."""

    account_id: uuid.UUID
    chat_id: str
    route: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    salon: Salon
    channel: Channel
    external_user_id: str  # ID пользователя в канале: сессия веб-чата, chat_id Telegram…
    text: str
    external_message_id: str | None = None  # ID сообщения в канале: защита от повторного вебхука
    client_name: str | None = None
    reply_to: ReplyRoute | None = None  # мессенджеры: ответ уходит через outbox


@dataclass(frozen=True, slots=True)
class Reply:
    text: str | None  # None — отвечать не нужно (повтор вебхука)
    conversation_id: uuid.UUID | None = None
    handed_off: bool = False
    duplicate: bool = False
    result: AgentResult | None = None
    outbox_key: str | None = None  # что доставить воркеру; при повторе задачи — тот же ключ


class ConversationService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        providers: ProviderRegistry,
        llm_clients: Mapping[str, LLMClient],
        default_models: list[str],
        notifier: Notifier,
        fallback_llm: LLMClient | None = None,
        embedder: EmbeddingsClient | None = None,
        tracer: Tracer | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.redis = redis
        self.providers = providers
        self.llm_clients = llm_clients
        self.default_models = default_models
        self.fallback_llm = fallback_llm
        self.notifier = notifier
        self.embedder = embedder or HashingEmbeddings()
        self.tracer = tracer or NoopTracer()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.states = StateStore(redis)
        self.budget = DailyBudget(redis)
        self.lock = RedisLock(redis)

    async def handle(self, message: IncomingMessage) -> Reply:
        salon = message.salon
        lock_name = key("lock", "dialog", salon.id, message.channel.value, message.external_user_id)
        async with (
            self.lock.hold(lock_name, ttl_s=LOCK_TTL_S, wait_s=LOCK_WAIT_S),
            self.session_factory() as session,
        ):
            reply = await self._handle(session, message)
            await session.commit()
        return reply

    @staticmethod
    def _outbox_key(message: IncomingMessage) -> str | None:
        if message.reply_to is None:
            return None
        return outbox_key(
            message.channel,
            message.reply_to.account_id,
            message.reply_to.chat_id,
            message.external_message_id or uuid.uuid4().hex,
        )

    async def _answer(
        self,
        session: AsyncSession,
        conversations: ConversationsRepository,
        conversation_id: uuid.UUID,
        message: IncomingMessage,
        text: str,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_rub: Decimal | None = None,
    ) -> str | None:
        """Сохраняет ответ агента и, для мессенджера, строку outbox — одной транзакцией."""
        await conversations.add_message(
            conversation_id, MessageRole.AGENT, text, tokens_in=tokens_in, tokens_out=tokens_out, cost_rub=cost_rub
        )
        delivery_key = self._outbox_key(message)
        if delivery_key and message.reply_to:
            await enqueue_outbound(
                session,
                salon_id=message.salon.id,
                account_id=message.reply_to.account_id,
                conversation_id=conversation_id,
                channel=message.channel,
                chat_id=message.reply_to.chat_id,
                route=message.reply_to.route,
                text=text,
                dedupe_key=delivery_key,
            )
        return delivery_key

    async def _handle(self, session: AsyncSession, message: IncomingMessage) -> Reply:
        salon = message.salon
        clients = ClientsRepository(session, salon.id)
        conversations = ConversationsRepository(session, salon.id)
        client = await clients.find_or_create(message.channel, message.external_user_id, name=message.client_name)
        conversation = await conversations.open_for(client.id, message.channel)
        conversation_id = conversation.id
        # Состояние — до сохранения нового сообщения: иначе восстановление из БД добавило бы
        # текущую реплику в историю, и агент увидел бы её дважды
        state = await self.states.load(
            str(salon.id), str(conversation_id), restore=lambda: conversations.restore_state(conversation_id)
        )
        stored = await conversations.add_message(
            conversation_id, MessageRole.CLIENT, message.text, external_id=message.external_message_id
        )
        if stored is None:
            # Повтор вебхука или задачи: ответ уже сохранён — воркер доставит его, если не успел
            return Reply(None, conversation_id, duplicate=True, outbox_key=self._outbox_key(message))
        if conversation.status is ConversationStatus.HANDED_OFF:
            await self.notifier.notify(
                AdminEvent(AdminEventKind.CLIENT_MESSAGE, str(salon.id), str(conversation_id), message.text)
            )
            return Reply(HANDED_OFF_ANSWER, conversation_id, handed_off=True)

        config = salon.config
        if await self.budget.exceeded(str(salon.id), config.daily_llm_budget_rub):
            return await self._budget_exceeded(session, conversations, conversation_id, message)

        try:
            router = build_router(
                self.llm_clients, config.llm_models or self.default_models, fallback=self.fallback_llm
            )
        except ValueError:  # ни одной настроенной модели — отвечает администратор
            logger.error("no_llm_configured", salon_id=str(salon.id))
            delivery_key = await self._answer(session, conversations, conversation_id, message, HANDOFF_ANSWER)
            await conversations.set_status(conversation_id, ConversationStatus.HANDED_OFF)
            await self.notifier.notify(
                AdminEvent(AdminEventKind.HANDOFF, str(salon.id), str(conversation_id), "Не настроена ни одна LLM")
            )
            return Reply(HANDOFF_ANSWER, conversation_id, handed_off=True, outbox_key=delivery_key)

        booking = BookingService(
            salon_id=str(salon.id),
            timezone=salon.timezone,
            provider=self.providers.for_salon(salon),
            redis=self.redis,
            ledger=SqlBookingLedger(session, salon.id),
        )
        ctx = ToolContext(
            salon_id=str(salon.id),
            salon_name=salon.name,
            conversation_id=str(conversation_id),
            now=self.clock().astimezone(ZoneInfo(salon.timezone)),
            booking=booking,
            catalog=DbCatalog(session, salon.id),
            salon_info=RagSalonInfo(HybridRetriever(session, salon.id, self.embedder)),
            client_id=client.id,
        )
        agent = Agent(router, default_registry(), prompt_version=config.prompt_version, tracer=self.tracer)
        result = await agent.run(message.text, ctx, state, profile=await clients.profile(client))

        delivery_key = await self._answer(
            session,
            conversations,
            conversation_id,
            message,
            result.text,
            tokens_in=result.usage.prompt_tokens,
            tokens_out=result.usage.completion_tokens,
            cost_rub=result.cost_rub,
        )
        await conversations.add_tool_calls(conversation_id, result.tool_calls)
        await conversations.set_summary(conversation_id, state.summary)
        await self.budget.add(str(salon.id), result.cost_rub)
        await self.states.save(str(salon.id), str(conversation_id), state)
        if result.handed_off:
            await conversations.set_status(conversation_id, ConversationStatus.HANDED_OFF)
            await self.notifier.notify(
                AdminEvent(AdminEventKind.HANDOFF, str(salon.id), str(conversation_id), result.handoff_reason or "")
            )
        if any(outcome.result and outcome.result.get("status") == "pending_admin" for outcome in result.tool_calls):
            await self.notifier.notify(
                AdminEvent(
                    AdminEventKind.BOOKING_PENDING,
                    str(salon.id),
                    str(conversation_id),
                    "CRM недоступна: подтвердите запись вручную",
                )
            )
        return Reply(result.text, conversation_id, handed_off=result.handed_off, result=result, outbox_key=delivery_key)

    async def _budget_exceeded(
        self,
        session: AsyncSession,
        conversations: ConversationsRepository,
        conversation_id: uuid.UUID,
        message: IncomingMessage,
    ) -> Reply:
        salon = message.salon
        logger.warning("llm_budget_exceeded", salon_id=str(salon.id))
        delivery_key = await self._answer(session, conversations, conversation_id, message, BUDGET_EXCEEDED_ANSWER)
        await conversations.set_status(conversation_id, ConversationStatus.HANDED_OFF)
        if await self.budget.first_alert_today(str(salon.id)):
            await self.notifier.notify(
                AdminEvent(
                    AdminEventKind.BUDGET_EXCEEDED,
                    str(salon.id),
                    str(conversation_id),
                    f"Дневной лимит расходов на LLM ({salon.config.daily_llm_budget_rub} ₽) исчерпан",
                )
            )
        return Reply(BUDGET_EXCEEDED_ANSWER, conversation_id, handed_off=True, outbox_key=delivery_key)
