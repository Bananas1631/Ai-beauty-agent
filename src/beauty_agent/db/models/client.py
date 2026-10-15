"""Клиент салона и его учётные записи в мессенджерах."""

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import Channel

# E.164: плюс, код страны и номер, до 15 цифр, без пробелов и скобок: +79161502239
E164_PATTERN = r"^\+[1-9][0-9]{6,14}$"


class Client(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Клиент одного салона. Тот же человек в другом салоне — другой клиент."""

    __tablename__ = "clients"
    __table_args__ = (
        UniqueConstraint("salon_id", "phone"),
        CheckConstraint(f"phone ~ '{E164_PATTERN}'", name="phone_e164"),
    )

    phone: Mapped[str | None] = mapped_column(String(16))  # None, пока номер не назван
    name: Mapped[str | None] = mapped_column(String(200))

    identities: Mapped[list["ClientIdentity"]] = relationship(
        back_populates="client",
        lazy="raise",
    )


class ClientIdentity(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Канал и ID пользователя в нём → клиент. Узнаём клиента в любом мессенджере."""

    __tablename__ = "client_identities"
    __table_args__ = (UniqueConstraint("salon_id", "channel", "external_user_id"),)

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),
        index=True,
    )
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))
    external_user_id: Mapped[str] = mapped_column(String(128))  # ID в мессенджере

    client: Mapped[Client] = relationship(back_populates="identities", lazy="raise")
