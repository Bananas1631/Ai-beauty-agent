"""Каталог и сведения о салоне для инструментов агента — из нашей БД (импорт из CRM).

Цены и мастеров агент берёт отсюда, а не из CRM на каждый вопрос: БД отвечает за миллисекунды
и не тратит лимит запросов YCLIENTS. Свободное время — всегда из CRM через движок записи.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import KnowledgeItem, Service
from beauty_agent.db.repositories.catalog import MastersRepository, ServicesRepository
from beauty_agent.domain.entities import MasterInfo, ServiceInfo


class DbCatalog:
    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.services_repo = ServicesRepository(session, salon_id)
        self.masters_repo = MastersRepository(session, salon_id)

    @staticmethod
    def _info(service: Service) -> ServiceInfo:
        return ServiceInfo(
            external_id=service.external_id,
            name=service.name,
            duration_min=service.duration_min,
            price_min=service.price_min,
            price_max=service.price_max,
            category=service.category,
        )

    async def services(self) -> list[ServiceInfo]:
        return [self._info(service) for service in await self.services_repo.active()]

    async def masters_for(self, service_id: str) -> list[MasterInfo]:
        service = await self.services_repo.by_external_id(service_id)
        if service is None or not service.is_active:
            return []
        return [
            MasterInfo(master.external_id, master.name, master.specialization, frozenset({service_id}))
            for master in await self.masters_repo.for_service(service.id)
        ]


class KnowledgeSalonInfo:
    """Сведения о салоне из базы знаний: пока — карточка филиала; с 3 ноября — гибридный поиск."""

    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.session = session
        self.salon_id = salon_id

    async def search(self, question: str) -> list[dict[str, str]]:
        statement = (
            select(KnowledgeItem)
            .where(KnowledgeItem.salon_id == self.salon_id)
            .order_by(KnowledgeItem.created_at)
            .limit(3)
        )
        return [{"title": item.title, "text": item.content[:1200]} for item in await self.session.scalars(statement)]
