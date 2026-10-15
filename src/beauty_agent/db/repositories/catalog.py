"""Запросы к каталогу салона: услуги, мастера и связи «мастер — услуга».

Upsert-запросы нужны импорту каталога и ночной синхронизации; поиск — инструментам агента.
Строки обновляются по (salon_id, external_id): ID из CRM стабилен, название может меняться.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import Boolean, delete, literal_column, select, update
from sqlalchemy.dialects.postgresql import insert

from beauty_agent.db.models import Master, MasterService, Service
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.entities import MasterInfo, ServiceInfo


@dataclass(frozen=True, slots=True)
class UpsertResult:
    ids: dict[str, uuid.UUID]  # external_id → id строки в нашей БД
    created: int
    updated: int


class ServicesRepository(SalonRepository[Service]):
    model = Service

    async def by_external_id(self, external_id: str) -> Service | None:
        return await self.session.scalar(self._select().filter_by(external_id=external_id))

    async def active(self) -> Sequence[Service]:
        return (await self.session.scalars(self._select().filter_by(is_active=True).order_by(Service.name))).all()

    async def upsert(self, services: Iterable[ServiceInfo]) -> UpsertResult:
        ids: dict[str, uuid.UUID] = {}
        created = 0
        for info in services:
            values = {
                "name": info.name[:200],
                "category": info.category[:200] if info.category else None,
                "price_min": info.price_min if info.price_min is not None else 0,
                "price_max": info.price_max if info.price_max is not None else (info.price_min or 0),
                "duration_min": info.duration_min,
                "is_active": True,
            }
            statement = (
                insert(Service)
                .values(salon_id=self.salon_id, external_id=info.external_id, **values)
                .on_conflict_do_update(index_elements=["salon_id", "external_id"], set_=values)
                # xmax = 0 у только что вставленной строки: так отличаем создание от обновления
                .returning(Service.id, literal_column("xmax = 0", type_=Boolean))
            )
            row = (await self.session.execute(statement)).one()
            ids[info.external_id] = row[0]
            created += bool(row[1])
        return UpsertResult(ids, created, len(ids) - created)

    async def deactivate_missing(self, present: Iterable[str]) -> int:
        """Услуги, исчезнувшие из CRM, не удаляются: на них ссылаются прошлые записи."""
        statement = (
            update(Service)
            .where(Service.salon_id == self.salon_id, Service.is_active, Service.external_id.not_in(list(present)))
            .values(is_active=False)
        )
        result = await self.session.execute(statement)
        return int(result.rowcount)  # type: ignore[attr-defined]  # у UPDATE результат — CursorResult


class MastersRepository(SalonRepository[Master]):
    model = Master

    async def by_external_id(self, external_id: str) -> Master | None:
        return await self.session.scalar(self._select().filter_by(external_id=external_id))

    async def active(self) -> Sequence[Master]:
        return (await self.session.scalars(self._select().filter_by(is_active=True).order_by(Master.name))).all()

    async def for_service(self, service_id: uuid.UUID) -> Sequence[Master]:
        """Активные мастера, которые делают услугу."""
        statement = (
            self._select()
            .join(MasterService, MasterService.master_id == Master.id)
            .where(MasterService.service_id == service_id, Master.is_active)
            .order_by(Master.name)
        )
        return (await self.session.scalars(statement)).all()

    async def upsert(self, masters: Iterable[MasterInfo]) -> UpsertResult:
        ids: dict[str, uuid.UUID] = {}
        created = 0
        for info in masters:
            values = {
                "name": info.name[:200],
                "specialization": info.specialization[:200] if info.specialization else None,
                "is_active": True,
            }
            statement = (
                insert(Master)
                .values(salon_id=self.salon_id, external_id=info.external_id, **values)
                .on_conflict_do_update(index_elements=["salon_id", "external_id"], set_=values)
                .returning(Master.id, literal_column("xmax = 0", type_=Boolean))
            )
            row = (await self.session.execute(statement)).one()
            ids[info.external_id] = row[0]
            created += bool(row[1])
        return UpsertResult(ids, created, len(ids) - created)

    async def deactivate_missing(self, present: Iterable[str]) -> int:
        statement = (
            update(Master)
            .where(Master.salon_id == self.salon_id, Master.is_active, Master.external_id.not_in(list(present)))
            .values(is_active=False)
        )
        result = await self.session.execute(statement)
        return int(result.rowcount)  # type: ignore[attr-defined]


class MasterServicesRepository(SalonRepository[MasterService]):
    model = MasterService

    async def replace_all(self, pairs: Iterable[tuple[uuid.UUID, uuid.UUID]]) -> int:
        """Пересобирает связи салона целиком: проще и надёжнее, чем вычислять разницу."""
        await self.session.execute(delete(MasterService).where(MasterService.salon_id == self.salon_id))
        rows = [
            {"salon_id": self.salon_id, "master_id": master_id, "service_id": service_id}
            for master_id, service_id in set(pairs)
        ]
        if rows:
            await self.session.execute(insert(MasterService), rows)
        return len(rows)

    async def pairs(self) -> set[tuple[uuid.UUID, uuid.UUID]]:
        statement = select(MasterService.master_id, MasterService.service_id).where(
            MasterService.salon_id == self.salon_id
        )
        return {(row[0], row[1]) for row in await self.session.execute(statement)}
