"""Сервис диалога — единая точка входа для веб-чата и мессенджеров.

Принимает сообщение, находит или создаёт клиента по ClientIdentity и диалог, запускает агента
и сохраняет ответ с токенами и стоимостью. Одно сообщение диалога обрабатывается за раз
(блокировка в Redis): второе ждёт первое, иначе агент ответил бы дважды на одно и то же.

Для мессенджеров ответ не отправляется отсюда, а записывается в outbox в той же транзакции,
что и сообщение агента: отправку делает воркер, и повтор задачи не даст второго ответа.
В теневом режиме вместо outbox — черновик для администратора (conversation/shadow_mode.py).

Согласие (152-ФЗ, domain/consent.py): без него телефон и e-mail вырезаются из сообщения до
сохранения и до LLM, запись недоступна, к первому ответу добавляется текст со ссылкой на политику.
Пока диалог у администратора, агент молчит: сообщения клиента пересылаются в админ-бот.

Транзакции короткие (нагрузочный тест, docs/performance.md): приём сообщения коммитится до вызова
агента, инструменты берут соединение только на время своего запроса, ответ и строка outbox
сохраняются отдельной транзакцией. Иначе соединение пула простаивало бы «idle in transaction»,
пока агент секундами ждёт модель, и при 10 одновременных диалогах вставали бы все запросы сервиса.
Если задача упала между приёмом и ответом, повтор видит сохранённое сообщение без ответа и отвечает.
"""

import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.knowledge_client import KnowledgeClient
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.adapters.llm.vision import PhotoDescription
from beauty_agent.agent.budget import BUDGET_EXCEEDED_ANSWER, DailyBudget
from beauty_agent.agent.cache import AnswerCache
from beauty_agent.agent.loop import HANDOFF_ANSWER, Agent, AgentResult
from beauty_agent.agent.registry import SalonInfoSource, ToolContext, ToolOutcome
from beauty_agent.agent.router import build_router
from beauty_agent.agent.state import StateStore
from beauty_agent.agent.tools import CONSENT_REQUIRED, default_registry
from beauty_agent.catalog.lookup import DbCatalog
from beauty_agent.conversation.handoff import hand_off
from beauty_agent.conversation.notifications import AdminEvent, AdminEventKind, Notifier
from beauty_agent.conversation.outbox import enqueue_outbound, outbox_exists, outbox_key
from beauty_agent.conversation.privacy import PrivacyService
from beauty_agent.core import metrics
from beauty_agent.core.redis import RedisLock, key
from beauty_agent.core.tracing import NoopTracer, Tracer
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.admins import DraftsRepository
from beauty_agent.db.repositories.bookings import SqlBookingLedger
from beauty_agent.db.repositories.clients import ClientsRepository
from beauty_agent.db.repositories.conversations import ConversationsRepository
from beauty_agent.db.repositories.knowledge import KnowledgeRepository
from beauty_agent.domain.booking_engine.service import BookingService
from beauty_agent.domain.consent import (
    WITHDRAWN_ANSWER,
    ConsentIntent,
    ConsentState,
    allows_personal_data,
    consent_request,
    detect_intent,
    strip_personal_data,
)
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole, MessageStatus, ReplyMode
from beauty_agent.domain.events import LlmCallFinished, LlmCallFinishedData, MessageReceived, MessageReceivedData
from beauty_agent.events.outbox import record_event
from beauty_agent.rag.embeddings import EmbeddingsClient, HashingEmbeddings
from beauty_agent.rag.remote import IndexerSalonInfo, KnowledgeVersion, ShadowSalonInfo
from beauty_agent.rag.retriever import HybridRetriever, RagSalonInfo

logger = structlog.get_logger(__name__)

HANDED_OFF_ANSWER = "Ваш вопрос уже у администратора — он ответит в ближайшее рабочее время."
PHOTO_HANDOFF_ANSWER = "Спасибо за фото! Передала его администратору — он подскажет, какая услуга подойдёт."
LOCK_TTL_S = 120.0  # дольше самого медленного ответа агента: 6 шагов по 15 секунд с запасом
LOCK_WAIT_S = 60.0


@dataclass(frozen=True, slots=True)
class ReplyRoute:
    """Куда отправить ответ в мессенджер: аккаунт канала салона и чат клиента."""

    account_id: uuid.UUID
    chat_id: str
    route: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"account_id": str(self.account_id), "chat_id": self.chat_id, "route": dict(self.route)}


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    salon: Salon
    channel: Channel
    external_user_id: str  # ID пользователя в канале: сессия веб-чата, chat_id Telegram…
    text: str
    external_message_id: str | None = None  # ID сообщения в канале: защита от повторного вебхука
    client_name: str | None = None
    reply_to: ReplyRoute | None = None  # мессенджеры: ответ уходит через outbox
    # REST API: сообщение уже сохранено accept() со статусом «в очереди» — ответить на него
    stored_message_id: uuid.UUID | None = None
    photo: PhotoDescription | None = None  # фото-референс, разобранный workers/photo.py
    # Фото, которое агент не разбирает (режим admin, модель не уверена): сразу администратору
    photo_handoff_reason: str | None = None


def message_received(
    salon_id: uuid.UUID, message_id: uuid.UUID, conversation_id: uuid.UUID, client_id: uuid.UUID, channel: Channel
) -> MessageReceived:
    """Событие без текста сообщения: только ID и канал."""
    return MessageReceived(
        salon_id=salon_id,
        data=MessageReceivedData(
            message_id=message_id, conversation_id=conversation_id, client_id=client_id, channel=channel
        ),
    )


def offers_shown(result: AgentResult) -> int:
    """Сколько вариантов времени агент показал клиенту за ход — шаг воронки «предложены варианты»."""
    return sum(
        len(outcome.result.get("options") or [])
        for outcome in result.tool_calls
        if outcome.name in ("find_free_time", "book") and outcome.result
    )


def llm_call_finished(salon_id: uuid.UUID, conversation_id: uuid.UUID, result: AgentResult) -> LlmCallFinished:
    return LlmCallFinished(
        salon_id=salon_id,
        data=LlmCallFinishedData(
            conversation_id=conversation_id,
            model=result.model,
            prompt_version=result.prompt_version,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
            cost_rub=result.cost_rub,
            latency_ms=result.latency_ms,
            tool_calls=len(result.tool_calls),
            offers_shown=offers_shown(result),
            cache_hit=result.cache_hit,
            handed_off=result.handed_off,
            guardrail=result.guardrail,
        ),
    )


@dataclass(frozen=True, slots=True)
class Reply:
    text: str | None  # None — отвечать не нужно (повтор вебхука)
    conversation_id: uuid.UUID | None = None
    handed_off: bool = False
    duplicate: bool = False
    result: AgentResult | None = None
    outbox_key: str | None = None  # что доставить воркеру; при повторе задачи — тот же ключ
    draft_id: uuid.UUID | None = None  # теневой режим: черновик для администратора вместо ответа


_PENDING_ACTIONS = {
    "book": "подтвердите запись вручную",
    "cancel_booking": "клиент просит отменить запись",
    "reschedule_booking": "клиент просит перенести запись",
}


def pending_admin_notes(outcomes: Sequence[ToolOutcome]) -> list[str]:
    """Что поручить администратору, когда CRM недоступна: запись, отмена или перенос — с деталями."""
    notes = []
    for outcome in outcomes:
        if not outcome.result or outcome.result.get("status") != "pending_admin":
            continue
        details = outcome.result.get("old") or outcome.result
        when = f": {details.get('service', '')}, {details.get('master', '')}, {details.get('when', '')}"
        notes.append(f"CRM недоступна, {_PENDING_ACTIONS.get(outcome.name, 'обработайте запрос')}{when}")
    return notes


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
        privacy: PrivacyService | None = None,
        answer_cache: AnswerCache | None = None,
        knowledge_backend: str = "local",
        knowledge_client: KnowledgeClient | None = None,
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
        self.privacy = privacy or PrivacyService(redis, public_url="http://localhost:8000")
        self.answer_cache = answer_cache
        # Сведения о сайте: свой модуль, теневое сравнение или индексатор (ADR 014)
        self.knowledge_backend = knowledge_backend if knowledge_client is not None else "local"
        self.knowledge_client = knowledge_client
        self.budget = DailyBudget(redis)
        self.lock = RedisLock(redis)

    async def handle(self, message: IncomingMessage) -> Reply:
        salon = message.salon
        lock_name = key("lock", "dialog", salon.id, message.channel.value, message.external_user_id)
        async with (
            self.lock.hold(lock_name, ttl_s=LOCK_TTL_S, wait_s=LOCK_WAIT_S),
            self.session_factory() as session,
        ):
            with metrics.reply_timer(message.channel.value):
                reply = await self._handle(session, message)
                await session.commit()
        if reply.result is not None:
            metrics.llm_cost(str(salon.id), reply.result.cost_rub)
        return reply

    def _salon_info(self, session: AsyncSession, salon_id: uuid.UUID) -> SalonInfoSource:
        local = HybridRetriever(session, salon_id, self.embedder)
        if self.knowledge_client is None or self.knowledge_backend == "local":
            return RagSalonInfo(local)
        if self.knowledge_backend == "shadow":
            return ShadowSalonInfo(self.knowledge_client, salon_id, local)
        return IndexerSalonInfo(self.knowledge_client, salon_id, local)

    def _knowledge_version(self, session: AsyncSession, salon_id: uuid.UUID) -> Callable[[], Awaitable[str | None]]:
        local = KnowledgeRepository(session, salon_id).version
        if self.knowledge_client is None or self.knowledge_backend != "indexer":
            return local
        return KnowledgeVersion(local, self.redis, self.knowledge_client, salon_id)

    async def accept(self, message: IncomingMessage) -> tuple[uuid.UUID, bool]:
        """REST API: сохранить сообщение клиента со статусом «в очереди»; ответ посчитает воркер.

        Без согласия телефон и e-mail вырезаются сразу — до записи в БД. Повтор запроса с тем же
        Idempotency-Key (external_message_id) возвращает уже сохранённое сообщение.
        """
        salon = message.salon
        async with self.session_factory() as session:
            clients = ClientsRepository(session, salon.id)
            conversations = ConversationsRepository(session, salon.id)
            client = await clients.find_or_create(message.channel, message.external_user_id, name=message.client_name)
            consent = await self.privacy.state(session, salon.id, client.id)
            text = message.text if allows_personal_data(consent) else strip_personal_data(message.text)
            conversation = await conversations.open_for(client.id, message.channel)
            stored = await conversations.add_message(
                conversation.id,
                MessageRole.CLIENT,
                text,
                external_id=message.external_message_id,
                status=MessageStatus.QUEUED,
            )
            if stored is None and message.external_message_id is not None:
                existing = await conversations.message_by_external_id(conversation.id, message.external_message_id)
                if existing is None:  # pragma: no cover — конфликт по external_id без строки невозможен
                    raise RuntimeError("Сообщение с этим ключом не найдено")
                await session.commit()
                return existing.id, False
            if stored is None:  # pragma: no cover — без external_id конфликта не бывает
                raise RuntimeError("Сообщение не сохранено")
            await record_event(session, message_received(salon.id, stored, conversation.id, client.id, message.channel))
            await session.commit()
            return stored, True

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
        question: str = "",
        question_id: uuid.UUID | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cost_rub: Decimal | None = None,
    ) -> tuple[str | None, uuid.UUID | None]:
        """Ответ агента одной транзакцией: сообщение и строка outbox, а в теневом режиме — черновик.

        Возвращает (ключ outbox, ID черновика).
        """
        delivery_key = self._outbox_key(message)
        if delivery_key and message.reply_to and message.salon.config.reply_mode is ReplyMode.SHADOW:
            drafts = DraftsRepository(session)
            draft_id = await drafts.create(
                message.salon.id,
                conversation_id,
                dedupe_key=delivery_key,
                question=question,
                text=text,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_rub=cost_rub,
            )
            if draft_id is None:
                existing = await drafts.by_key(message.salon.id, delivery_key)
                draft_id = existing.id if existing else None
            return None, draft_id
        await conversations.add_message(
            conversation_id,
            MessageRole.AGENT,
            text,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_rub=cost_rub,
            in_reply_to=question_id,
        )
        if message.stored_message_id is not None:
            await conversations.set_message_status(message.stored_message_id, MessageStatus.DONE)
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
        return delivery_key, None

    async def _withdraw(self, session: AsyncSession, message: IncomingMessage, client_id: uuid.UUID) -> Reply:
        """Отзыв согласия: данные удаляются, подтверждение уходит клиенту сразу, без черновика."""
        await self.privacy.withdraw(session, message.salon.id, client_id, message.channel)
        delivery_key = self._outbox_key(message)
        if delivery_key and message.reply_to:
            await enqueue_outbound(
                session,
                salon_id=message.salon.id,
                account_id=message.reply_to.account_id,
                conversation_id=None,  # диалог удалён вместе с перепиской
                channel=message.channel,
                chat_id=message.reply_to.chat_id,
                route=message.reply_to.route,
                text=WITHDRAWN_ANSWER,
                dedupe_key=delivery_key,
            )
        return Reply(WITHDRAWN_ANSWER, None, outbox_key=delivery_key)

    async def _handle(self, session: AsyncSession, message: IncomingMessage) -> Reply:
        salon = message.salon
        clients = ClientsRepository(session, salon.id)
        conversations = ConversationsRepository(session, salon.id)
        client = await clients.find_or_create(message.channel, message.external_user_id, name=message.client_name)
        intent = detect_intent(message.text)
        if intent is ConsentIntent.WITHDRAW:
            return await self._withdraw(session, message, client.id)
        consent = await self.privacy.state(session, salon.id, client.id)
        if intent is ConsentIntent.GRANT and consent is not ConsentState.GRANTED:
            await self.privacy.grant(session, salon.id, client.id, message.channel)
            consent = ConsentState.GRANTED
        allowed = allows_personal_data(consent)
        # Без согласия телефон и e-mail не сохраняются и не уходят в LLM
        text = message.text if allowed else strip_personal_data(message.text)

        conversation = await conversations.open_for(client.id, message.channel)
        conversation_id = conversation.id
        # Состояние — до сохранения нового сообщения: иначе восстановление из БД добавило бы
        # текущую реплику в историю, и агент увидел бы её дважды
        state = await self.states.load(
            str(salon.id), str(conversation_id), restore=lambda: conversations.restore_state(conversation_id)
        )
        if message.stored_message_id is not None:
            stored: uuid.UUID | None = message.stored_message_id
            earlier = await conversations.reply_of(message.stored_message_id)
            if earlier is not None:  # повтор задачи Celery: ответ уже сохранён
                return Reply(earlier.content, conversation_id, duplicate=True)
            await conversations.set_message_status(message.stored_message_id, MessageStatus.PROCESSING)
            # сообщение уже в БД: если состояние восстановлено оттуда, оно уже в истории
            if state.turns and state.turns[-1].role == "user" and state.turns[-1].content == text:
                state.turns.pop()
        else:
            stored = await conversations.add_message(
                conversation_id, MessageRole.CLIENT, text, external_id=message.external_message_id
            )
            if stored is not None:
                await record_event(
                    session, message_received(salon.id, stored, conversation_id, client.id, message.channel)
                )
        if stored is None:
            delivery_key = self._outbox_key(message)
            draft = await DraftsRepository(session).by_key(salon.id, delivery_key) if delivery_key else None
            answered = draft is not None or (
                delivery_key is not None and await outbox_exists(session, salon.id, delivery_key)
            )
            if answered or delivery_key is None:
                # Повтор вебхука или задачи: ответ уже сохранён — воркер доставит его, если не успел
                return Reply(
                    None,
                    conversation_id,
                    duplicate=True,
                    outbox_key=None if draft else delivery_key,
                    draft_id=draft.id if draft else None,
                )
            # Прошлая попытка сохранила сообщение, но упала до ответа — отвечаем сейчас.
            # Если состояние восстановлено из БД, сообщение уже в истории — второй раз его не добавляем.
            if state.turns and state.turns[-1].role == "user" and state.turns[-1].content == text:
                state.turns.pop()
        if message.reply_to is not None:
            await conversations.set_route(conversation_id, message.reply_to.to_json())
        if conversation.status is ConversationStatus.HANDED_OFF:
            await self.notifier.notify(
                AdminEvent(AdminEventKind.CLIENT_MESSAGE, str(salon.id), str(conversation_id), text)
            )
            # Мессенджер: агент молчит, отвечает администратор из админ-бота. Виджету сайта
            # администратор ответить не может — клиент там хотя бы узнает, что его услышали
            answer = HANDED_OFF_ANSWER if message.reply_to is None else None
            if message.stored_message_id is not None and answer is not None:
                await self._answer(session, conversations, conversation_id, message, answer, question_id=stored)
            return Reply(answer, conversation_id, handed_off=True)

        if message.photo_handoff_reason is not None:
            delivery_key, draft_id = await self._answer(
                session,
                conversations,
                conversation_id,
                message,
                PHOTO_HANDOFF_ANSWER,
                question=text,
                question_id=stored,
            )
            await hand_off(
                session,
                self.notifier,
                salon.id,
                conversation_id,
                message.photo_handoff_reason,
                kind=AdminEventKind.PHOTO_REFERENCE,
            )
            return Reply(
                PHOTO_HANDOFF_ANSWER, conversation_id, handed_off=True, outbox_key=delivery_key, draft_id=draft_id
            )

        config = salon.config
        if await self.budget.exceeded(str(salon.id), config.daily_llm_budget_rub):
            return await self._budget_exceeded(session, conversations, conversation_id, message, text, stored)

        try:
            router = build_router(
                self.llm_clients, config.llm_models or self.default_models, fallback=self.fallback_llm
            )
        except ValueError:  # ни одной настроенной модели — отвечает администратор
            logger.error("no_llm_configured", salon_id=str(salon.id))
            delivery_key, draft_id = await self._answer(
                session, conversations, conversation_id, message, HANDOFF_ANSWER, question=text, question_id=stored
            )
            await hand_off(session, self.notifier, salon.id, conversation_id, "Не настроена ни одна LLM")
            return Reply(HANDOFF_ANSWER, conversation_id, handed_off=True, outbox_key=delivery_key, draft_id=draft_id)

        profile = await clients.profile(client)
        first_reply = await conversations.count_messages(conversation_id, MessageRole.AGENT) == 0
        # Фаза приёма закончена: соединение возвращается в пул на время работы агента
        await session.commit()

        async with self.session_factory() as tool_session:

            async def release() -> None:
                """После каждого инструмента: закончить его транзакцию и отдать соединение в пул."""
                try:
                    await tool_session.commit()
                except Exception:
                    await tool_session.rollback()

            booking = BookingService(
                salon_id=str(salon.id),
                timezone=salon.timezone,
                provider=self.providers.for_salon(salon),
                redis=self.redis,
                ledger=SqlBookingLedger(
                    tool_session,
                    salon.id,
                    timezone=salon.timezone,
                    reminders=config.reminders_enabled,
                    clock=self.clock,
                ),
            )
            ctx = ToolContext(
                salon_id=str(salon.id),
                salon_name=salon.name,
                conversation_id=str(conversation_id),
                now=self.clock().astimezone(ZoneInfo(salon.timezone)),
                booking=booking,
                catalog=DbCatalog(tool_session, salon.id),
                salon_info=self._salon_info(tool_session, salon.id),
                client_id=client.id,
                release=release,
                knowledge_version=self._knowledge_version(tool_session, salon.id),
                photo=message.photo,
            )
            agent = Agent(
                router,
                default_registry(personal_data_allowed=allowed),
                prompt_version=config.prompt_version,
                tracer=self.tracer,
                cache=self.answer_cache,
            )
            result = await agent.run(text, ctx, state, profile=profile)
        answer = result.text
        # Просьба о согласии — к первому ответу и когда агент пытался записать; не при передаче администратору
        if (
            not allowed
            and not result.handed_off
            and (
                first_reply
                or any(
                    outcome.result and outcome.result.get("status") == CONSENT_REQUIRED for outcome in result.tool_calls
                )
            )
        ):
            answer = f"{answer}\n\n{consent_request(self.privacy.policy_url(salon))}"

        delivery_key, draft_id = await self._answer(
            session,
            conversations,
            conversation_id,
            message,
            answer,
            question=text,
            question_id=stored,
            tokens_in=result.usage.prompt_tokens,
            tokens_out=result.usage.completion_tokens,
            cost_rub=result.cost_rub,
        )
        await conversations.add_tool_calls(conversation_id, result.tool_calls)
        await record_event(session, llm_call_finished(salon.id, conversation_id, result))
        await conversations.set_summary(conversation_id, state.summary)
        await self.budget.add(str(salon.id), result.cost_rub)
        await self.states.save(str(salon.id), str(conversation_id), state)
        if result.handed_off:
            await hand_off(session, self.notifier, salon.id, conversation_id, result.handoff_reason or "")
        for note in pending_admin_notes(result.tool_calls):
            await self.notifier.notify(
                AdminEvent(AdminEventKind.BOOKING_PENDING, str(salon.id), str(conversation_id), note)
            )
        return Reply(
            answer,
            conversation_id,
            handed_off=result.handed_off,
            result=result,
            outbox_key=delivery_key,
            draft_id=draft_id,
        )

    async def _budget_exceeded(
        self,
        session: AsyncSession,
        conversations: ConversationsRepository,
        conversation_id: uuid.UUID,
        message: IncomingMessage,
        question: str,
        question_id: uuid.UUID | None = None,
    ) -> Reply:
        salon = message.salon
        logger.warning("llm_budget_exceeded", salon_id=str(salon.id))
        delivery_key, draft_id = await self._answer(
            session,
            conversations,
            conversation_id,
            message,
            BUDGET_EXCEEDED_ANSWER,
            question=question,
            question_id=question_id,
        )
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
        return Reply(
            BUDGET_EXCEEDED_ANSWER, conversation_id, handed_off=True, outbox_key=delivery_key, draft_id=draft_id
        )
