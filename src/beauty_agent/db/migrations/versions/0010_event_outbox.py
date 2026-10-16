"""event outbox

Таблица transactional outbox для доменных событий (domain/events.py → Kafka): событие пишется
в той же транзакции, что и запись, relay публикует его и отмечает published_at. Новая таблица —
старый код её не видит, миграция обратно совместима.

Revision ID: 0010
Create Date: 2026-12-23 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_event_outbox_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_event_outbox_dedupe_key")),
        sa.UniqueConstraint("event_id", name=op.f("uq_event_outbox_event_id")),
    )
    op.create_index(
        "ix_event_outbox_pending",
        "event_outbox",
        ["salon_id", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(op.f("ix_event_outbox_published_at"), "event_outbox", ["published_at"])
    op.create_index(op.f("ix_event_outbox_salon_id"), "event_outbox", ["salon_id"])


def downgrade() -> None:
    op.drop_index(op.f("ix_event_outbox_salon_id"), table_name="event_outbox")
    op.drop_index(op.f("ix_event_outbox_published_at"), table_name="event_outbox")
    op.drop_index(
        "ix_event_outbox_pending", table_name="event_outbox", postgresql_where=sa.text("published_at IS NULL")
    )
    op.drop_table("event_outbox")
