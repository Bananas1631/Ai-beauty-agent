"""Журнал согласий клиентов (152-ФЗ): кто, на что, по какой версии документа и когда."""

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import Channel, ConsentType


class Consent(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Событие журнала: согласие дано или отозвано. Строки только добавляются, не меняются."""

    __tablename__ = "consents"

    client_id: Mapped[uuid.UUID] = mapped_column(
        # доказательство согласия не должно пропасть вместе с клиентом случайно
        ForeignKey("clients.id", ondelete="RESTRICT"),
        index=True,
    )
    kind: Mapped[ConsentType] = mapped_column(str_enum(ConsentType, "kind"))
    granted: Mapped[bool]  # True — согласие дано, False — отозвано
    document_version: Mapped[str] = mapped_column(String(32))  # версия текста согласия
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))  # где получено
