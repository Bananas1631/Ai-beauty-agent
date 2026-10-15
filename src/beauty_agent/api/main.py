"""Точка входа веб-приложения: uvicorn вызывает create_app() при старте (--factory).

Подключения к БД и Redis открываются в lifespan при старте и закрываются при остановке:
импорт модуля ничего не подключает, а тесты передают свои настройки в create_app(settings).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from beauty_agent import __version__
from beauty_agent.api import health, v1
from beauty_agent.api.errors import install_error_handlers
from beauty_agent.api.middleware import TraceIdMiddleware
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.db import make_engine, make_session_factory
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.redis import make_redis
from beauty_agent.core.sentry import init_sentry


def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика: настройки и логи готовятся при вызове, а не при импорте модуля."""
    settings = settings or get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    init_sentry(settings.sentry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = make_engine(settings.postgres)
        redis = make_redis(settings.redis)
        app.state.engine = engine
        app.state.session_factory = make_session_factory(engine)
        app.state.redis = redis
        try:
            yield
        finally:
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

    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1.router)
    return app
