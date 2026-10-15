"""Диалоги, сообщения и журнал вызовов инструментов агента."""

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import Channel, ConversationStatus, MessageRole


class Conversation(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Диалог клиента с салоном в одном канале."""

    __tablename__ = "conversations"

    client_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"),  # переписка — персональные данные клиента
        index=True,
    )
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))
    status: Mapped[ConversationStatus] = mapped_column(
        str_enum(ConversationStatus, "status"),
        server_default=ConversationStatus.ACTIVE.value,
    )
    summary: Mapped[str | None] = mapped_column(Text)  # резюме старых реплик (21 октября)


class Message(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Сообщение в диалоге: от клиента, агента или администратора."""

    __tablename__ = "messages"
    __table_args__ = (
        # повторный вебхук с тем же сообщением не создаст дубль; ID сообщения уникален в чате
        UniqueConstraint("conversation_id", "external_id"),
        # история диалога по порядку и постранично
        Index(None, "conversation_id", "created_at"),
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    role: Mapped[MessageRole] = mapped_column(str_enum(MessageRole, "role"))
    content: Mapped[str] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(String(128))  # ID сообщения в мессенджере
    # расход на ответ агента; у сообщений клиента пусто
    tokens_in: Mapped[int | None]
    tokens_out: Mapped[int | None]
    cost_rub: Mapped[Decimal | None] = mapped_column(Numeric(10, 4))


class ToolCall(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Вызов инструмента агентом: что спросил, что получил, сколько ждал. Для разбора ответов."""

    __tablename__ = "tool_calls"
    __table_args__ = (Index(None, "salon_id", "created_at"),)  # вызовы салона за период

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64))
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    duration_ms: Mapped[int]
