"""Записи клиентов в PostgreSQL.

Сохранение — одна транзакция на уровне READ COMMITTED. От гонки двух подтверждений защищают
уникальный ключ идемпотентности и ограничение исключения ex_bookings_master_overlap,
а не SERIALIZABLE с повторами (docs/adr/006-hybrid-code-llm.md).
"""

import uuid

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Booking
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.db.repositories.catalog import MastersRepository, ServicesRepository
from beauty_agent.domain.booking_engine.service import BookingEntry

logger = structlog.get_logger(__name__)


class BookingsRepository(SalonRepository[Booking]):
    model = Booking

    async def by_idempotency_key(self, idempotency_key: str) -> Booking | None:
        return await self.session.scalar(self._select().filter_by(idempotency_key=idempotency_key))


class SqlBookingLedger:
    """Реализация BookingLedger: сохраняет запись движка в таблицу bookings."""

    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.session = session
        self.salon_id = salon_id
        self.bookings = BookingsRepository(session, salon_id)

    async def record(self, entry: BookingEntry) -> None:
        if await self.bookings.by_idempotency_key(entry.idempotency_key) is not None:
            return  # повтор того же подтверждения: запись уже сохранена
        master = await MastersRepository(self.session, self.salon_id).by_external_id(entry.slot.master_id)
        service = await ServicesRepository(self.session, self.salon_id).by_external_id(entry.slot.service_id)
        if master is None or service is None:
            # Каталог салона ещё не импортирован: в CRM запись есть, связать её с нашими строками нечем
            logger.warning("booking_not_in_catalog", master_id=entry.slot.master_id, service_id=entry.slot.service_id)
            return
        self.bookings.add(
            Booking(
                client_id=entry.client_id,
                master_id=master.id,
                service_id=service.id,
                starts_at=entry.slot.starts_at,
                ends_at=entry.slot.ends_at,
                status=entry.status,
                price=entry.price,
                external_id=entry.external_id,
                idempotency_key=entry.idempotency_key,
                created_by_ai=True,
            )
        )
        try:
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            if "uq_bookings_salon_id_idempotency_key" in str(exc.orig):
                return  # параллельное подтверждение успело раньше — запись одна
            # Пересечение с записью в нашей БД: CRM — источник правды, у нас устаревшая копия
            logger.error("booking_overlaps_in_db", external_id=entry.external_id)
            raise
