"""Подключение к PostgreSQL: фабрики движка и сессий.

Движок создаётся при старте приложения (с 9 октября — в lifespan),
а не при импорте модуля: импорт ничего не подключает, тесты передают свои настройки.
"""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from beauty_agent.core.config import PostgresSettings


def make_engine(settings: PostgresSettings, *, echo: bool = False, migrations: bool = False) -> AsyncEngine:
    """Движок — пул соединений с базой, один на всё приложение.

    migrations=True — без statement_timeout: CREATE INDEX CONCURRENTLY на большой таблице идёт минуты.
    """
    server_settings = {
        "statement_timeout": "0" if migrations else str(settings.statement_timeout_ms),
        "idle_in_transaction_session_timeout": str(settings.idle_in_transaction_timeout_ms),
        "application_name": "beauty-agent-migrations" if migrations else "beauty-agent",
    }
    return create_async_engine(
        settings.url,
        echo=echo,  # True — печатать каждый SQL-запрос, только для отладки
        connect_args={"server_settings": server_settings, "timeout": settings.connect_timeout_s},
        pool_size=settings.pool_size,  # постоянных соединений в пуле
        max_overflow=settings.max_overflow,  # временных сверх них при пиковой нагрузке
        pool_timeout=settings.pool_timeout_s,
        pool_pre_ping=True,  # перед выдачей проверить, что соединение живо
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий. Сессия — единица работы с базой (Unit of Work)."""
    return async_sessionmaker(engine, expire_on_commit=False)
