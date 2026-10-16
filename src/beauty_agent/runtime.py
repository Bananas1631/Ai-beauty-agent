"""Сборка приложения: подключения, адаптеры и сервисы — одним кодом для API и воркеров Celery.

API создаёт Runtime в lifespan, каждый процесс воркера — при старте. У каждого процесса свои
движок БД, пул Redis и HTTP-клиент: их нельзя делить между процессами и циклами событий.
"""

from dataclasses import dataclass, field
from typing import Any

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.admin_bot import AdminBot, AdminBotApi, AdminBotNotifier
from beauty_agent.adapters.channels.base import ChannelSender
from beauty_agent.adapters.channels.max import MaxBotApi, tls_context
from beauty_agent.adapters.channels.telegram import TelegramBotApi
from beauty_agent.adapters.channels.vk import VkApi
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.latency import SlowLLM
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.adapters.llm.models import build_clients, build_embedder, build_vision
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.agent.cache import AnswerCache
from beauty_agent.agent.state import StateStore
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.conversation.handoff import Handoff
from beauty_agent.conversation.inbound import InboundGate, InboundProcessor, MessageQueue
from beauty_agent.conversation.notifications import LogNotifier, Notifier
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.conversation.photo import PhotoProcessor
from beauty_agent.conversation.privacy import PrivacyService
from beauty_agent.conversation.service import ConversationService
from beauty_agent.conversation.shadow_mode import DraftPublisher, ShadowMode
from beauty_agent.core.config import Settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.redis import make_redis
from beauty_agent.core.resilience import RedisTokenBucket
from beauty_agent.core.security import Security
from beauty_agent.core.tracing import Tracer, make_tracer
from beauty_agent.domain.enums import Channel
from beauty_agent.rag.safe_http import make_safe_client

YCLIENTS_RATE_KEY = "ba:yclients:rps"  # лимит YCLIENTS — на IP сервера, общий для всех процессов


class NullQueue:
    """Очередь, которая ничего не ставит: процессы, не принимающие вебхуки."""

    async def schedule(self, *args: object, **kwargs: object) -> None:
        return None

    async def submit_admin_update(self, payload: dict[str, Any]) -> None:
        return None

    async def sync_booking(self, *args: object) -> None:
        return None

    async def process_chat(self, *args: object) -> None:
        return None

    async def reindex_site(self, *args: object) -> None:
        return None

    async def process_photo(self, *args: object) -> None:
        return None


@dataclass
class Runtime:
    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    http: httpx.AsyncClient
    security: Security
    providers: ProviderRegistry
    llm_clients: dict[str, LLMClient]
    tracer: Tracer
    notifier: Notifier
    conversations: ConversationService
    debouncer: Debouncer
    outbox: Outbox
    telegram: TelegramBotApi
    vk: VkApi
    max_bot: MaxBotApi
    max_http: httpx.AsyncClient
    privacy: PrivacyService
    shadow: ShadowMode
    handoff: Handoff
    queue: MessageQueue
    senders: dict[Channel, ChannelSender] = field(default_factory=dict)
    inbound: InboundGate | None = None
    processor: InboundProcessor | None = None
    photo: PhotoProcessor | None = None
    admin_bot: AdminBot | None = None

    async def close(self) -> None:
        self.tracer.flush()
        await self.providers.aclose()
        for client in self.llm_clients.values():
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        await self.http.aclose()
        await self.max_http.aclose()
        if self.photo is not None:
            await self.photo.http.aclose()
            close_vision = getattr(self.photo.vision, "aclose", None)
            if close_vision is not None:
                await close_vision()
        await self.redis.aclose()
        await self.engine.dispose()


def create_runtime(
    settings: Settings,
    *,
    queue: MessageQueue | None = None,
    notifier: Notifier | None = None,
    http: httpx.AsyncClient | None = None,
) -> Runtime:
    engine = make_engine(settings.postgres)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings.redis)
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    # MAX работает на сертификате Минцифры: отдельный клиент со своим набором корневых сертификатов
    max_http = httpx.AsyncClient(
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        verify=tls_context(settings.max_bot.ca_bundle),
    )
    security = Security(settings.security, env=settings.app.env)
    limiter = RedisTokenBucket(redis, YCLIENTS_RATE_KEY, settings.yclients.requests_per_second)
    simulated = settings.app.simulated_latency_ms if settings.app.env != "prod" else 0
    providers = ProviderRegistry(settings.yclients, limiter=limiter, fake_latency_ms=simulated // 5)
    # Без ключей LLM вне прода отвечает офлайн-модель: демо и проверка работают сразу
    fallback: LLMClient | None = None
    if settings.app.env != "prod":
        fallback = SlowLLM(OfflineLLM(), simulated) if simulated else OfflineLLM()
    llm_clients = build_clients(settings.llm)
    tracer = make_tracer(settings.langfuse)
    telegram = TelegramBotApi(http)
    admin_api = AdminBotApi(telegram, settings.admin_bot.token.get_secret_value()) if settings.admin_bot.token else None
    admin_notifier = AdminBotNotifier(admin_api, session_factory, redis) if admin_api else None
    notifier = notifier or admin_notifier or LogNotifier()
    privacy = PrivacyService(redis, public_url=settings.app.public_url)
    embedder = build_embedder(settings.llm)
    answer_cache = (
        AnswerCache(redis, semantic=settings.llm.semantic_cache, embedder=embedder)
        if settings.llm.answer_cache
        else None
    )
    conversations = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=providers,
        llm_clients=llm_clients,
        default_models=settings.llm.default_models,
        notifier=notifier,
        fallback_llm=fallback,
        embedder=embedder,
        tracer=tracer,
        privacy=privacy,
        answer_cache=answer_cache,
    )
    vk = VkApi(http)
    max_bot = MaxBotApi(max_http, base_url=settings.max_bot.api_url)
    senders: dict[Channel, ChannelSender] = {
        Channel.TELEGRAM: telegram,
        Channel.TELEGRAM_BUSINESS: telegram,
        Channel.VK: vk,
        Channel.MAX: max_bot,
    }
    debouncer = Debouncer(redis)
    outbox = Outbox(session_factory, security, senders)
    states = StateStore(redis)
    publisher: DraftPublisher | None = admin_notifier
    shadow = ShadowMode(session_factory, outbox, states, publisher)
    handoff = Handoff(session_factory, outbox, states)
    queue = queue or NullQueue()
    runtime = Runtime(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        http=http,
        security=security,
        providers=providers,
        llm_clients=llm_clients,
        tracer=tracer,
        notifier=notifier,
        conversations=conversations,
        debouncer=debouncer,
        outbox=outbox,
        telegram=telegram,
        vk=vk,
        max_bot=max_bot,
        max_http=max_http,
        privacy=privacy,
        shadow=shadow,
        handoff=handoff,
        queue=queue,
        senders=senders,
    )
    runtime.inbound = InboundGate(redis, debouncer, queue)
    runtime.processor = InboundProcessor(
        session_factory, debouncer, conversations, outbox, notifier, shadow, queue=queue
    )
    runtime.photo = PhotoProcessor(
        session_factory=session_factory,
        security=security,
        conversations=conversations,
        outbox=outbox,
        telegram=telegram,
        vision=build_vision(settings.llm),
        http=make_safe_client(timeout_s=20.0),
        shadow=shadow,
    )
    if admin_api is not None:
        runtime.admin_bot = AdminBot(
            api=admin_api,
            session_factory=session_factory,
            redis=redis,
            security=security,
            shadow=shadow,
            handoff=handoff,
            username=settings.admin_bot.username,
        )
    return runtime
