"""Запись клиента к мастеру. База сама не даст записать к мастеру двоих на одно время."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint, column, false, func
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import BookingStatus, ReminderStatus


class Booking(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Запись: клиент, мастер, услуга, время и статус."""

    __tablename__ = "bookings"
    __table_args__ = (
        CheckConstraint("starts_at < ends_at", name="time_order"),
        UniqueConstraint("salon_id", "idempotency_key"),
        UniqueConstraint("salon_id", "external_id"),
        # Интервалы времени одного мастера не пересекаются; отменённые записи время не держат.
        # Для «=» по UUID в индексе GiST нужно расширение btree_gist — его включает миграция.
        ExcludeConstraint(
            (column("master_id"), "="),
            (func.tstzrange(column("starts_at"), column("ends_at")), "&&"),
            where=column("status") != BookingStatus.CANCELLED.value,
            using="gist",
            name="ex_bookings_master_overlap",
        ),
    )

    # клиента можно удалить по требованию 152-ФЗ, а запись останется для отчётов и счёта
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"),
        index=True,
    )
    # диалог, из которого записались: туда уходят напоминания; удалён диалог — напоминаний нет
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"),
        index=True,
    )
    master_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("masters.id", ondelete="RESTRICT"))
    service_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("services.id", ondelete="RESTRICT"))
    starts_at: Mapped[datetime]
    ends_at: Mapped[datetime]
    status: Mapped[BookingStatus] = mapped_column(str_enum(BookingStatus, "status"))
    price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # цена на момент записи
    external_id: Mapped[str | None] = mapped_column(String(64))  # ID записи в CRM
    # повтор запроса с тем же ключом не создаёт вторую запись
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_by_ai: Mapped[bool] = mapped_column(server_default=false())  # для счёта салону


class Reminder(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Напоминание о записи. Отправляет периодическая задача, а не задача Celery с ETA:
    RabbitMQ 4 отключает потребителя, который держит неподтверждённое сообщение дольше 30 минут."""

    __tablename__ = "reminders"
    __table_args__ = (
        UniqueConstraint("booking_id", "kind"),
        # выборка «пора отправлять» по частичному индексу только ожидающих
        Index(None, "due_at", postgresql_where=column("status") == ReminderStatus.PENDING.value),
    )

    booking_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bookings.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(8))  # 24h | 2h
    due_at: Mapped[datetime]
    status: Mapped[ReminderStatus] = mapped_column(
        str_enum(ReminderStatus, "status"),
        server_default=ReminderStatus.PENDING.value,
    )
    sent_at: Mapped[datetime | None]
