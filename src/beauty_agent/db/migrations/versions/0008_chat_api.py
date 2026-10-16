"""chat api

REST API для внешних интеграций: ключи API салона (только хеш), статус обработки сообщения
и ссылка ответа агента на вопрос клиента; новый канал api в CHECK-ограничениях колонок channel.

Revision ID: 0008
Create Date: 2026-10-16 09:21:40.827934
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHANNEL_TABLES = ("channel_accounts", "client_identities", "consents", "conversations", "outbound_messages")
OLD_CHANNELS = ("telegram", "telegram_business", "vk", "max", "web")
NEW_CHANNELS = (*OLD_CHANNELS, "api")


def _channel_check(table: str, values: tuple[str, ...]) -> None:
    """Перечисление — VARCHAR с CHECK (db/base.py: str_enum): новое значение — новым CHECK."""
    allowed = ", ".join(f"'{value}'" for value in values)
    op.drop_constraint(op.f(f"ck_{table}_channel"), table, type_="check")
    op.create_check_constraint(op.f(f"ck_{table}_channel"), table, f"channel IN ({allowed})")


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("prefix", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_api_keys_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_keys")),
        sa.UniqueConstraint("key_hash", name=op.f("uq_api_keys_key_hash")),
    )
    op.create_index(op.f("ix_api_keys_salon_id"), "api_keys", ["salon_id"], unique=False)
    op.add_column(
        "messages",
        sa.Column(
            "status",
            sa.Enum(
                "queued",
                "processing",
                "done",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
                length=32,
            ),
            nullable=True,
        ),
    )
    op.add_column("messages", sa.Column("error", sa.String(length=300), nullable=True))
    op.add_column("messages", sa.Column("in_reply_to", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_messages_in_reply_to"), "messages", ["in_reply_to"], unique=False)
    op.create_foreign_key(
        op.f("fk_messages_in_reply_to_messages"), "messages", "messages", ["in_reply_to"], ["id"], ondelete="SET NULL"
    )
    for table in CHANNEL_TABLES:
        _channel_check(table, NEW_CHANNELS)


def downgrade() -> None:
    for table in CHANNEL_TABLES:
        _channel_check(table, OLD_CHANNELS)
    op.drop_constraint(op.f("fk_messages_in_reply_to_messages"), "messages", type_="foreignkey")
    op.drop_index(op.f("ix_messages_in_reply_to"), table_name="messages")
    op.drop_column("messages", "in_reply_to")
    op.drop_column("messages", "error")
    op.drop_column("messages", "status")
    op.drop_index(op.f("ix_api_keys_salon_id"), table_name="api_keys")
    op.drop_table("api_keys")
