"""Исходящие сообщения в мессенджеры (outbox): отметка «отправлено» защищает от двойного ответа.

Задачи Celery доставляются «хотя бы один раз»: воркер мог отправить ответ и упасть до подтверждения
задачи. Перед отправкой воркер атомарно занимает строку; отправленную строку повтор не тронет.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy import text as sql
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import Channel, OutboxStatus


class OutboundMessage(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    __tablename__ = "outbound_messages"
    __table_args__ = (
        UniqueConstraint("salon_id", "dedupe_key"),
        # подбор «зависших» отправок: статус sending с истёкшей блокировкой
        Index(None, "status", "locked_until"),
    )

    channel_account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channel_accounts.id", ondelete="CASCADE"))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    channel: Mapped[Channel] = mapped_column(str_enum(Channel, "channel"))
    chat_id: Mapped[str] = mapped_column(String(128))
    route: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=sql("'{}'::jsonb"))  # business_connection_id…
    text: Mapped[str] = mapped_column(Text)
    # один ответ на одно входящее сообщение: канал + аккаунт + чат + ID входящего
    dedupe_key: Mapped[str] = mapped_column(String(300))
    status: Mapped[OutboxStatus] = mapped_column(
        str_enum(OutboxStatus, "status"), server_default=OutboxStatus.PENDING.value
    )
    attempts: Mapped[int] = mapped_column(server_default=sql("0"))
    locked_until: Mapped[datetime | None]
    sent_at: Mapped[datetime | None]
    external_id: Mapped[str | None] = mapped_column(String(128))  # ID отправленного сообщения в мессенджере
    error: Mapped[str | None] = mapped_column(String(500))
