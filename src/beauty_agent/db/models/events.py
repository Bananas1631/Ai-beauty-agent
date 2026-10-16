"""Таблицы потребителей событий: идемпотентность и накопленная статистика."""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey


class ProcessedEvent(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Событие обработано группой потребителей: строка пишется в одной транзакции с результатом.

    Повторная доставка (падение до commit offset, повтор relay) натыкается на уникальный ключ
    и не даёт второго эффекта. Строки старше срока хранения Kafka удаляются: дубля старше
    в журнале быть не может.
    """

    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("consumer_group", "event_id"),)

    consumer_group: Mapped[str] = mapped_column(String(100))
    event_id: Mapped[uuid.UUID]


class SalonWeekStats(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Статистика салона за неделю (с понедельника, по времени салона) — для еженедельного отчёта.

    Копит потребитель events/handlers/reports.py из событий, а не ночной SQL по всей БД.
    """

    __tablename__ = "salon_week_stats"
    __table_args__ = (UniqueConstraint("salon_id", "week_start"),)

    week_start: Mapped[date]
    ai_bookings: Mapped[int] = mapped_column(default=0, server_default="0")
    cancelled_bookings: Mapped[int] = mapped_column(default=0, server_default="0")
    attributed_visits: Mapped[int] = mapped_column(default=0, server_default="0")
    messages_received: Mapped[int] = mapped_column(default=0, server_default="0")
    answers: Mapped[int] = mapped_column(default=0, server_default="0")
    off_hours_answers: Mapped[int] = mapped_column(default=0, server_default="0")
    llm_cost_rub: Mapped[Decimal] = mapped_column(Numeric(12, 4), default=Decimal(0), server_default="0")
