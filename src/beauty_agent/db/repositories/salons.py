"""Репозиторий салонов. Таблица salons сама задаёт арендатора, поэтому фильтра по salon_id здесь нет."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Salon


class SalonsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, salon_id: uuid.UUID) -> Salon | None:
        return await self.session.get(Salon, salon_id)

    async def get_by_slug(self, slug: str) -> Salon | None:
        """Салон по адресу страницы: /s/{slug} и /api/v1/salons/{slug}."""
        return await self.session.scalar(select(Salon).where(Salon.slug == slug))

    async def get_by_yclients_id(self, company_id: int) -> Salon | None:
        return await self.session.scalar(select(Salon).where(Salon.yclients_company_id == company_id))

    def add(self, salon: Salon) -> Salon:
        self.session.add(salon)
        return salon
