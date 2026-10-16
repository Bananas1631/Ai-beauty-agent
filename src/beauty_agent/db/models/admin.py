"""Администраторы салона в админ-боте и черновики ответов теневого режима."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import AdminRole, DraftStatus


class SalonAdmin(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Владелец или администратор салона, привязанный к админ-боту одноразовым кодом."""

    __tablename__ = "salon_admins"
    __table_args__ = (UniqueConstraint("salon_id", "telegram_user_id"),)

    telegram_user_id: Mapped[str] = mapped_column(String(32), index=True)
    chat_id: Mapped[str] = mapped_column(String(32))  # личный чат с админ-ботом
    name: Mapped[str | None] = mapped_column(String(200))
    role: Mapped[AdminRole] = mapped_column(str_enum(AdminRole, "role"))
    is_active: Mapped[bool] = mapped_column(server_default=text("true"))


class Draft(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Черновик ответа агента: администратор отправляет, исправляет или отвечает сам."""

    __tablename__ = "drafts"
    __table_args__ = (
        UniqueConstraint("salon_id", "dedupe_key"),
        Index(None, "salon_id", "created_at"),  # отчёт точности за период
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(300))  # повтор задачи не создаёт второй черновик
    question: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[DraftStatus] = mapped_column(
        str_enum(DraftStatus, "status"),
        server_default=DraftStatus.PENDING.value,
    )
    final_text: Mapped[str | None] = mapped_column(Text)
    similarity: Mapped[int | None]  # 0–100, rapidfuzz: насколько исправленный текст похож на черновик
    decided_by: Mapped[str | None] = mapped_column(String(32))  # Telegram ID администратора
    decided_at: Mapped[datetime | None]
    notified_at: Mapped[datetime | None]  # когда черновик показан администраторам
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    cost_rub: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))
