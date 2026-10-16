"""Transactional outbox доменных событий: событие пишется в одной транзакции с изменением данных.

Relay (events/outbox_relay.py) публикует строки в Kafka по порядку id и отмечает published_at.
Без outbox запись в БД и сообщение в Kafka могли бы разойтись: запись сохранилась, а событие —
нет (или наоборот). PostgreSQL — источник правды: потерянное в Kafka публикуется заново отсюда.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, ForeignKey, Identity, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base

PENDING = text("published_at IS NULL")


class EventOutbox(Base):
    __tablename__ = "event_outbox"
    __table_args__ = (
        # очередь relay: неопубликованные события салона по порядку
        Index("ix_event_outbox_pending", "salon_id", "id", postgresql_where=PENDING),
        # очистка опубликованного старше срока хранения Kafka
        Index(None, "published_at"),
    )

    # BIGINT по возрастанию, а не UUID: relay публикует события салона в порядке записи
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    salon_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("salons.id", ondelete="RESTRICT"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    topic: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    # событие, которое должно случиться один раз (booking.attributed:<id>): повтор не пишется
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    published_at: Mapped[datetime | None]
