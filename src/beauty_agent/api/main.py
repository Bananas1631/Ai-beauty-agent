"""Точка входа веб-приложения: uvicorn вызывает create_app() при старте.

Пока это заготовка с одним эндпоинтом /health; 9 октября здесь появятся
подключения к БД и Redis, /ready и trace_id для каждого запроса.
"""

from fastapi import FastAPI

from beauty_agent import __version__
from beauty_agent.core.config import get_settings
from beauty_agent.core.logging import configure_logging


def create_app() -> FastAPI:
    """Фабрика: настройки и логи готовятся при вызове, а не при импорте модуля."""
    settings = get_settings()
    configure_logging(settings.app.log_level, json_logs=settings.app.log_json)

    app = FastAPI(title="beauty-agent", version=__version__)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Процесс жив. Доступность БД и Redis будет проверять /ready (9 октября)."""
        return {"status": "ok"}

    return app
