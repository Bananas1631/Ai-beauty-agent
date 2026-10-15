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


def make_engine(settings: PostgresSettings, *, echo: bool = False) -> AsyncEngine:
    """Движок — пул соединений с базой, один на всё приложение."""
    return create_async_engine(
        settings.url,
        echo=echo,  # True — печатать каждый SQL-запрос, только для отладки
        pool_size=5,  # постоянных соединений в пуле
        max_overflow=5,  # временных сверх них при пиковой нагрузке
        pool_pre_ping=True,  # перед выдачей проверить, что соединение живо
    )


def make_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий. Сессия — единица работы с базой (Unit of Work)."""
    return async_sessionmaker(engine, expire_on_commit=False)
