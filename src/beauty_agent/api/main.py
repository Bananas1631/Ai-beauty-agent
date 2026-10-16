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
from beauty_agent.api import health, metrics, pages, v1, widget_ws
from beauty_agent.api.errors import install_error_handlers
from beauty_agent.api.middleware import TraceIdMiddleware
from beauty_agent.api.security import SecurityHeadersMiddleware
from beauty_agent.core.config import Settings, get_settings
from beauty_agent.core.logging import configure_logging
from beauty_agent.core.metrics import MetricsMiddleware
from beauty_agent.core.sentry import init_sentry
from beauty_agent.runtime import create_runtime

STATIC = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    """Фабрика: настройки и логи готовятся при вызове, а не при импорте модуля."""
    settings = settings or get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)
    init_sentry(settings.sentry)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        from beauty_agent.workers.queue import CeleryQueue  # Celery нужен только для публикации задач

        runtime = create_runtime(settings, queue=CeleryQueue())
        app.state.runtime = runtime
        app.state.engine = runtime.engine
        app.state.session_factory = runtime.session_factory
        app.state.redis = runtime.redis
        app.state.security = runtime.security
        app.state.conversations = runtime.conversations
        app.state.inbound = runtime.inbound
        try:
            yield
        finally:
            await runtime.close()

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
    app.add_middleware(MetricsMiddleware)
    app.add_middleware(TraceIdMiddleware)
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(metrics.router)
    app.include_router(v1.router)
    app.include_router(pages.router)
    app.include_router(widget_ws.router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app
