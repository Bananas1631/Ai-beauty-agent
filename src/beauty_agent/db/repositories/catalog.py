"""Запросы к каталогу салона: услуги, мастера и связи «мастер — услуга»."""

from beauty_agent.db.models import Master, Service
from beauty_agent.db.repositories.base import SalonRepository


class ServicesRepository(SalonRepository[Service]):
    model = Service

    async def by_external_id(self, external_id: str) -> Service | None:
        return await self.session.scalar(self._select().filter_by(external_id=external_id))


class MastersRepository(SalonRepository[Master]):
    model = Master

    async def by_external_id(self, external_id: str) -> Master | None:
        return await self.session.scalar(self._select().filter_by(external_id=external_id))
