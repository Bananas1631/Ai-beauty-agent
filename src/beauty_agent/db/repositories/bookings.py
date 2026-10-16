"""Записи клиентов в PostgreSQL.

Сохранение — одна транзакция на уровне READ COMMITTED. От гонки двух подтверждений защищают
уникальный ключ идемпотентности и ограничение исключения ex_bookings_master_overlap,
а не SERIALIZABLE с повторами (docs/adr/006-hybrid-code-llm.md).

Вместе с записью: имя и телефон — в карточку клиента (запись возможна только с согласием),
напоминания — если они включены у салона. Отмена снимает напоминания, перенос — переставляет.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import structlog
from sqlalchemy import exists, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from beauty_agent.db.models import Booking, Client, Master, Service
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.db.repositories.catalog import MastersRepository, ServicesRepository
from beauty_agent.db.repositories.reminders import RemindersRepository
from beauty_agent.domain.booking_engine.service import BookingEntry, StoredBooking
from beauty_agent.domain.entities import ClientContact, Slot
from beauty_agent.domain.enums import BookingStatus
from beauty_agent.domain.events import (
    BookingCancelled,
    BookingCreated,
    BookingData,
    BookingRescheduled,
    BookingRescheduledData,
)
from beauty_agent.events.outbox import record_event

logger = structlog.get_logger(__name__)


class BookingsRepository(SalonRepository[Booking]):
    model = Booking

    async def by_idempotency_key(self, idempotency_key: str) -> Booking | None:
        return await self.session.scalar(self._select().filter_by(idempotency_key=idempotency_key))

    async def by_external_id(self, external_id: str) -> Booking | None:
        return await self.session.scalar(self._select().filter_by(external_id=external_id))


def _uuid_or_none(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def booking_data(booking: Booking) -> BookingData:
    """Данные события о записи: только ID, время, цена и статус — без имени и телефона клиента."""
    return BookingData(
        booking_id=booking.id,
        external_id=booking.external_id,
        client_id=booking.client_id,
        conversation_id=booking.conversation_id,
        service_id=booking.service_id,
        master_id=booking.master_id,
        starts_at=booking.starts_at,
        ends_at=booking.ends_at,
        price=booking.price,
        status=booking.status,
        created_by_ai=booking.created_by_ai,
    )


class SqlBookingLedger:
    """Реализация BookingLedger: сохраняет запись движка в таблицу bookings.

    Каждое изменение пишет доменное событие в event_outbox той же транзакцией (events/outbox.py).
    """

    def __init__(
        self,
        session: AsyncSession,
        salon_id: uuid.UUID,
        *,
        timezone: str = "Europe/Moscow",
        reminders: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.salon_id = salon_id
        self.timezone = timezone
        self.reminders_enabled = reminders
        self.clock = clock or (lambda: datetime.now(UTC))
        self.bookings = BookingsRepository(session, salon_id)
        self.reminders = RemindersRepository(session, salon_id)

    async def _fill_client_card(self, client_id: uuid.UUID, contact: ClientContact) -> None:
        """Имя и телефон из записи — в пустые поля карточки. Номер другой карточки салона не трогаем."""
        other = aliased(Client)
        await self.session.execute(
            update(Client)
            .where(
                Client.salon_id == self.salon_id,
                Client.id == client_id,
                Client.phone.is_(None),
                ~exists().where(other.salon_id == self.salon_id, other.phone == contact.phone),
            )
            .values(phone=contact.phone)
        )
        await self.session.execute(
            update(Client)
            .where(Client.salon_id == self.salon_id, Client.id == client_id, Client.name.is_(None))
            .values(name=contact.name[:200])
        )

    async def record(self, entry: BookingEntry) -> None:
        if await self.bookings.by_idempotency_key(entry.idempotency_key) is not None:
            return  # повтор того же подтверждения: запись уже сохранена
        master = await MastersRepository(self.session, self.salon_id).by_external_id(entry.slot.master_id)
        service = await ServicesRepository(self.session, self.salon_id).by_external_id(entry.slot.service_id)
        if master is None or service is None:
            # Каталог салона ещё не импортирован: в CRM запись есть, связать её с нашими строками нечем
            logger.warning("booking_not_in_catalog", master_id=entry.slot.master_id, service_id=entry.slot.service_id)
            return
        booking = self.bookings.add(
            Booking(
                client_id=entry.client_id,
                conversation_id=_uuid_or_none(entry.conversation_id),
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
            await self.session.flush()
            if entry.client_id is not None and entry.client is not None:
                await self._fill_client_card(entry.client_id, entry.client)
            if self.reminders_enabled and entry.status is BookingStatus.CONFIRMED:
                await self.reminders.plan(booking.id, booking.starts_at, self.clock(), self.timezone)
            await record_event(self.session, BookingCreated(salon_id=self.salon_id, data=booking_data(booking)))
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            if "uq_bookings_salon_id_idempotency_key" in str(exc.orig):
                return  # параллельное подтверждение успело раньше — запись одна
            # Пересечение с записью в нашей БД: CRM — источник правды, у нас устаревшая копия
            logger.error("booking_overlaps_in_db", external_id=entry.external_id)
            raise

    # ------------------------------------------------------------- мои записи, отмена, перенос

    async def upcoming(self, client_id: uuid.UUID, now: datetime) -> list[StoredBooking]:
        statement = (
            select(Booking, Master.external_id, Service.external_id)
            .join(Master, Master.id == Booking.master_id)
            .join(Service, Service.id == Booking.service_id)
            .where(
                Booking.salon_id == self.salon_id,
                Booking.client_id == client_id,
                Booking.status.in_([BookingStatus.CONFIRMED, BookingStatus.PENDING]),
                Booking.starts_at > now,
            )
            .order_by(Booking.starts_at)
            .limit(10)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            StoredBooking(
                external_id=booking.external_id,
                slot=Slot(
                    str(master_external),
                    str(service_external),
                    booking.starts_at,
                    max(int((booking.ends_at - booking.starts_at).total_seconds() // 60), 1),
                ),
                status=booking.status,
            )
            for booking, master_external, service_external in rows
        ]

    async def cancel(self, external_id: str) -> None:
        booking = await self.bookings.by_external_id(external_id)
        if booking is None or booking.status is BookingStatus.CANCELLED:
            return
        booking.status = BookingStatus.CANCELLED
        await self.reminders.cancel_pending(booking.id)
        await record_event(self.session, BookingCancelled(salon_id=self.salon_id, data=booking_data(booking)))
        await self.session.commit()

    async def move(self, external_id: str, slot: Slot) -> None:
        """Перенос в нашей копии. CRM — источник правды: пересечение в нашей БД пишем в лог, а не бросаем."""
        booking = await self.bookings.by_external_id(external_id)
        master = await MastersRepository(self.session, self.salon_id).by_external_id(slot.master_id)
        if booking is None or master is None:
            logger.warning("booking_move_not_found", external_id=external_id)
            return
        previous_starts_at, previous_master_id = booking.starts_at, booking.master_id
        booking.master_id = master.id
        booking.starts_at = slot.starts_at
        booking.ends_at = slot.ends_at
        try:
            await self.session.flush()
            await self.reminders.cancel_pending(booking.id)
            if self.reminders_enabled and booking.status is BookingStatus.CONFIRMED:
                await self.reminders.plan(booking.id, slot.starts_at, self.clock(), self.timezone)
            data = BookingRescheduledData(
                **booking_data(booking).model_dump(),
                previous_starts_at=previous_starts_at,
                previous_master_id=previous_master_id,
            )
            await record_event(self.session, BookingRescheduled(salon_id=self.salon_id, data=data))
            await self.session.commit()
        except IntegrityError:
            await self.session.rollback()
            logger.error("booking_move_overlaps_in_db", external_id=external_id)
