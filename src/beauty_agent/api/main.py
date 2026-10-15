"""Точка входа веб-приложения: uvicorn вызывает create_app() при старте (--factory).

Подключения к БД и Redis, провайдеры CRM и клиенты LLM создаются в lifespan при старте
и закрываются при остановке: импорт модуля ничего не подключает, а тесты передают свои
настройки в create_app(settings).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from beauty_agent import __version__
from beauty_agent.adapters.crm.factory import ProviderRegistry
from beauty_agent.adapters.llm.models import build_clients
from beauty_agent.adapters.llm.offline import OfflineLLM
from beauty_agent.api import health, pages, v1, widget_ws
from beauty_agent.api.errors import install_error_handlers
from beauty_agent.api.middleware import TraceIdMiddleware
from beauty_agent.api.security import SecurityHeadersMiddleware
from beauty_agent.conversation.notifications import LogNotifier
from beauty_agent.conversation.service import ConversationService
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.redis import make_redis
from beauty_agent.core.sentry import init_sentry

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика: настройки и логи готовятся при вызове, а не при импорте модуля."""
    settings = settings or get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    init_sentry(settings.sentry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.postgres)
        redis = make_redis(settings.redis)
        session_factory = make_session_factory(engine)
        providers = ProviderRegistry(settings.yclients)
        llm_clients = build_clients(settings.llm)
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.redis = redis
        app.state.conversations = ConversationService(
            session_factory=session_factory,
            redis=redis,
            providers=providers,
            llm_clients=llm_clients,
            default_models=settings.llm.default_models,
            notifier=LogNotifier(),
            # Без ключей LLM вне прода отвечает офлайн-модель: демо и проверка работают сразу
            fallback_llm=None if settings.app.env == "prod" else OfflineLLM(),
        )
        try:
            yield
        finally:
            await providers.aclose()
            for client in llm_clients.values():
                close = getattr(client, "aclose", None)
                if close is not None:
                    await close()
            await redis.aclose()
            await engine.dispose()

    docs = settings.app.docs_enabled
    app = FastAPI(
        title="beauty-agent",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if docs else None,
    )
    app.state.settings = settings

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.router)
    app.include_router(pages.router)
    app.include_router(widget_ws.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
