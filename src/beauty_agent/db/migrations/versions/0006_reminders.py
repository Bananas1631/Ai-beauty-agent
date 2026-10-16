"""reminders

Напоминания о записях за 24 и 2 часа и связь записи с диалогом, из которого записались:
напоминание уходит в тот же мессенджер.

Revision ID: 0006
Create Date: 2026-10-16 04:22:31.482292
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("booking_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=8), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "cancelled",
                "skipped",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["bookings.id"], name=op.f("fk_reminders_booking_id_bookings"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_reminders_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reminders")),
        sa.UniqueConstraint("booking_id", "kind", name=op.f("uq_reminders_booking_id_kind")),
    )
    op.create_index(
        op.f("ix_reminders_due_at"),
        "reminders",
        ["due_at"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(op.f("ix_reminders_salon_id"), "reminders", ["salon_id"], unique=False)
    op.add_column("bookings", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_bookings_conversation_id"), "bookings", ["conversation_id"], unique=False)
    op.create_foreign_key(
        op.f("fk_bookings_conversation_id_conversations"),
        "bookings",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_bookings_conversation_id_conversations"), "bookings", type_="foreignkey")
    op.drop_index(op.f("ix_bookings_conversation_id"), table_name="bookings")
    op.drop_column("bookings", "conversation_id")
    op.drop_index(op.f("ix_reminders_salon_id"), table_name="reminders")
    op.drop_index(op.f("ix_reminders_due_at"), table_name="reminders", postgresql_where=sa.text("status = 'pending'"))
    op.drop_table("reminders")
