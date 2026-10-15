"""Зависимости эндпоинтов: настройки, сессия БД, Redis и текущий салон.

Эндпоинты получают всё готовым через Depends, а тесты подменяют это через app.dependency_overrides.
"""

from collections.abc import AsyncIterator
from typing import Annotated

import structlog
from fastapi import Depends, Path, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.core.config import Settings
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.salons import SalonsRepository
from beauty_agent.domain.errors import NotFoundError

# Тот же формат, что у CHECK slug_format в таблице salons; длина — как у колонки
SLUG_PATTERN = r"^[a-z0-9-]+$"


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> Redis:
    redis: Redis = request.app.state.redis
    return redis


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """Сессия на запрос. Что не закоммичено явно, откатывается при выходе."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RedisDep = Annotated[Redis, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
SlugPath = Annotated[str, Path(min_length=1, max_length=64, pattern=SLUG_PATTERN)]


async def get_salon(slug: SlugPath, session: SessionDep) -> Salon:
    salon = await SalonsRepository(session).get_by_slug(slug)
    if salon is None:
        raise NotFoundError("Салон не найден")
    structlog.contextvars.bind_contextvars(salon_id=str(salon.id))
    return salon


SalonDep = Annotated[Salon, Depends(get_salon)]
