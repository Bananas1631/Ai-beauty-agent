"""event consumers

processed_events — идемпотентность потребителей событий (группа + event_id, пишется в одной
транзакции с результатом); salon_week_stats — недельная статистика салона, которую копит
потребитель отчётов вместо ночных SQL-запросов по всей БД. Новые таблицы — обратно совместимо.

Revision ID: 0011
Create Date: 2026-12-24 10:00:00
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "processed_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_group", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_processed_events_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processed_events")),
        sa.UniqueConstraint("consumer_group", "event_id", name=op.f("uq_processed_events_consumer_group_event_id")),
    )
    op.create_index(op.f("ix_processed_events_salon_id"), "processed_events", ["salon_id"], unique=False)
    op.create_table(
        "salon_week_stats",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("week_start", sa.Date(), nullable=False),
        sa.Column("ai_bookings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_bookings", sa.Integer(), server_default="0", nullable=False),
        sa.Column("attributed_visits", sa.Integer(), server_default="0", nullable=False),
        sa.Column("messages_received", sa.Integer(), server_default="0", nullable=False),
        sa.Column("answers", sa.Integer(), server_default="0", nullable=False),
        sa.Column("off_hours_answers", sa.Integer(), server_default="0", nullable=False),
        sa.Column("llm_cost_rub", sa.Numeric(precision=12, scale=4), server_default="0", nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_salon_week_stats_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salon_week_stats")),
        sa.UniqueConstraint("salon_id", "week_start", name=op.f("uq_salon_week_stats_salon_id_week_start")),
    )
    op.create_index(op.f("ix_salon_week_stats_salon_id"), "salon_week_stats", ["salon_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_salon_week_stats_salon_id"), table_name="salon_week_stats")
    op.drop_table("salon_week_stats")
    op.drop_index(op.f("ix_processed_events_salon_id"), table_name="processed_events")
    op.drop_table("processed_events")
