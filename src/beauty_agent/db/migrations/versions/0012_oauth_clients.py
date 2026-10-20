"""oauth clients

oauth_clients — клиенты OAuth2 client credentials: партнёрские интеграции салона и внутренние
сервисы (индексатор, админка, биллинг). Секрет хранится только хешем argon2id; webhook_url —
адрес для подписанных событий о записях. Новая таблица — обратно совместимо.

Revision ID: 0012
Create Date: 2026-12-28 10:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("secret_hash", sa.String(length=200), nullable=False),
        sa.Column("scopes", postgresql.ARRAY(sa.String(length=32)), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=True),
        sa.Column("webhook_url", sa.String(length=2048), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_oauth_clients_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_clients")),
        sa.UniqueConstraint("client_id", name=op.f("uq_oauth_clients_client_id")),
    )
    op.create_index(op.f("ix_oauth_clients_salon_id"), "oauth_clients", ["salon_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_oauth_clients_salon_id"), table_name="oauth_clients")
    op.drop_table("oauth_clients")
