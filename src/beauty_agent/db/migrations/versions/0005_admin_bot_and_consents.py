"""admin bot and consents

Админ-бот: администраторы салона и черновики теневого режима. У диалога — время последнего
сообщения (инбокс с keyset-пагинацией, срок хранения) и маршрут ответа в мессенджер.

Revision ID: 0005
Create Date: 2026-10-16 03:50:48.250565
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "salon_admins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("telegram_user_id", sa.String(length=32), nullable=False),
        sa.Column("chat_id", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=True),
        sa.Column(
            "role",
            sa.Enum("owner", "admin", name="role", native_enum=False, create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_salon_admins_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_salon_admins")),
        sa.UniqueConstraint("salon_id", "telegram_user_id", name=op.f("uq_salon_admins_salon_id_telegram_user_id")),
    )
    op.create_index(op.f("ix_salon_admins_salon_id"), "salon_admins", ["salon_id"], unique=False)
    op.create_index(op.f("ix_salon_admins_telegram_user_id"), "salon_admins", ["telegram_user_id"], unique=False)
    op.create_table(
        "drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=300), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "edited",
                "manual",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("final_text", sa.Text(), nullable=True),
        sa.Column("similarity", sa.Integer(), nullable=True),
        sa.Column("decided_by", sa.String(length=32), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=True),
        sa.Column("tokens_out", sa.Integer(), nullable=True),
        sa.Column("cost_rub", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_drafts_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_drafts_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_drafts")),
        sa.UniqueConstraint("salon_id", "dedupe_key", name=op.f("uq_drafts_salon_id_dedupe_key")),
    )
    op.create_index(op.f("ix_drafts_conversation_id"), "drafts", ["conversation_id"], unique=False)
    op.create_index(op.f("ix_drafts_salon_id"), "drafts", ["salon_id"], unique=False)
    op.create_index(op.f("ix_drafts_salon_id_created_at"), "drafts", ["salon_id", "created_at"], unique=False)
    op.add_column(
        "conversations",
        sa.Column("last_message_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.add_column("conversations", sa.Column("reply_route", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    # существующим диалогам — время их последнего сообщения, а не время миграции
    op.execute(
        """
        UPDATE conversations c
        SET last_message_at = m.last_at
        FROM (SELECT conversation_id, max(created_at) AS last_at FROM messages GROUP BY conversation_id) m
        WHERE m.conversation_id = c.id
        """
    )
    op.create_index(
        op.f("ix_conversations_salon_id_last_message_at_id"),
        "conversations",
        ["salon_id", "last_message_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_conversations_salon_id_last_message_at_id"), table_name="conversations")
    op.drop_column("conversations", "reply_route")
    op.drop_column("conversations", "last_message_at")
    op.drop_index(op.f("ix_drafts_salon_id_created_at"), table_name="drafts")
    op.drop_index(op.f("ix_drafts_salon_id"), table_name="drafts")
    op.drop_index(op.f("ix_drafts_conversation_id"), table_name="drafts")
    op.drop_table("drafts")
    op.drop_index(op.f("ix_salon_admins_telegram_user_id"), table_name="salon_admins")
    op.drop_index(op.f("ix_salon_admins_salon_id"), table_name="salon_admins")
    op.drop_table("salon_admins")
