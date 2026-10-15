"""Каталог салона: услуги, мастера и кто какие услуги делает. Цены агент берёт отсюда."""

import uuid
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, String, UniqueConstraint, true
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey


class Service(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Услуга салона: «Маникюр с покрытием», 90 минут, от 2500 до 3000 ₽."""

    __tablename__ = "services"
    __table_args__ = (
        UniqueConstraint("salon_id", "external_id"),
        CheckConstraint("price_min >= 0 AND price_min <= price_max", name="price_range"),
        CheckConstraint("duration_min > 0", name="duration_positive"),
    )

    external_id: Mapped[str] = mapped_column(String(64))  # ID услуги в CRM
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str | None] = mapped_column(String(200))
    # Numeric — точное десятичное число: 2500.00 хранится ровно, без ошибок округления float
    price_min: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    price_max: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    duration_min: Mapped[int]
    # исчезла из CRM — не удаляем, а выключаем: на услугу ссылаются прошлые записи
    is_active: Mapped[bool] = mapped_column(server_default=true())


class Master(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Мастер салона."""

    __tablename__ = "masters"
    __table_args__ = (UniqueConstraint("salon_id", "external_id"),)

    external_id: Mapped[str] = mapped_column(String(64))  # ID мастера в CRM
    name: Mapped[str] = mapped_column(String(200))
    specialization: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(server_default=true())


class MasterService(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Мастер делает услугу. Пара «мастер + услуга» встречается один раз."""

    __tablename__ = "master_services"
    __table_args__ = (UniqueConstraint("master_id", "service_id"),)

    master_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"))
    service_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"),
        index=True,  # «кто делает эту услугу»; поиск по мастеру обслуживает уникальный индекс
    )
