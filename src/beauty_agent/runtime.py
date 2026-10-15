"""Сборка приложения: подключения, адаптеры и сервисы — одним кодом для API и воркеров Celery.

API создаёт Runtime в lifespan, каждый процесс воркера — при старте. У каждого процесса свои
движок БД, пул Redis и HTTP-клиент: их нельзя делить между процессами и циклами событий.
"""

from dataclasses import dataclass, field

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from beauty_agent.adapters.channels.base import ChannelSender
from beauty_agent.adapters.channels.telegram import TelegramBotApi
from beauty_agent.adapters.channels.vk import VkApi
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.llm.base import LLMClient
from beauty_agent.adapters.llm.models import build_clients, build_embedder
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.conversation.debounce import Debouncer
from beauty_agent.conversation.inbound import InboundGate, InboundProcessor, MessageQueue
from beauty_agent.conversation.notifications import LogNotifier, Notifier
from beauty_agent.conversation.outbox import Outbox
from beauty_agent.conversation.service import ConversationService
from beauty_agent.core.config import Settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.redis import make_redis
from beauty_agent.core.resilience import RedisTokenBucket
from beauty_agent.core.security import Security
from beauty_agent.core.tracing import Tracer, make_tracer
from beauty_agent.domain.enums import Channel

YCLIENTS_RATE_KEY = "ba:yclients:rps"  # лимит YCLIENTS — на IP сервера, общий для всех процессов


class NullQueue:
    """Очередь, которая ничего не ставит: процессы, не принимающие вебхуки."""

    async def schedule(self, *args: object, **kwargs: object) -> None:
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
    senders: dict[Channel, ChannelSender] = field(default_factory=dict)
    inbound: InboundGate | None = None
    processor: InboundProcessor | None = None

    async def close(self) -> None:
        self.tracer.flush()
        await self.providers.aclose()
        for client in self.llm_clients.values():
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
        await self.http.aclose()
        await self.redis.aclose()
        await self.engine.dispose()


def create_runtime(
    settings: Settings, *, queue: MessageQueue | None = None, notifier: Notifier | None = None
) -> Runtime:
    engine = make_engine(settings.postgres)
    session_factory = make_session_factory(engine)
    redis = make_redis(settings.redis)
    http = httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0), follow_redirects=False)
    security = Security(settings.security, env=settings.app.env)
    limiter = RedisTokenBucket(redis, YCLIENTS_RATE_KEY, settings.yclients.requests_per_second)
    providers = ProviderRegistry(settings.yclients, limiter=limiter)
    llm_clients = build_clients(settings.llm)
    tracer = make_tracer(settings.langfuse)
    notifier = notifier or LogNotifier()
    conversations = ConversationService(
        session_factory=session_factory,
        redis=redis,
        providers=providers,
        llm_clients=llm_clients,
        default_models=settings.llm.default_models,
        notifier=notifier,
        # Без ключей LLM вне прода отвечает офлайн-модель: демо и проверка работают сразу
        fallback_llm=None if settings.app.env == "prod" else OfflineLLM(),
        embedder=build_embedder(settings.llm),
        tracer=tracer,
    )
    telegram = TelegramBotApi(http)
    vk = VkApi(http)
    senders: dict[Channel, ChannelSender] = {
        Channel.TELEGRAM: telegram,
        Channel.TELEGRAM_BUSINESS: telegram,
        Channel.VK: vk,
    }
    debouncer = Debouncer(redis)
    outbox = Outbox(session_factory, security, senders)
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
        senders=senders,
    )
    runtime.inbound = InboundGate(redis, debouncer, queue or NullQueue())
    runtime.processor = InboundProcessor(session_factory, debouncer, conversations, outbox, notifier)
    return runtime
