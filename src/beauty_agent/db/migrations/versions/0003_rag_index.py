"""rag index

Фрагменты базы знаний для поиска: колонка эмбеддинга (pgvector, 1024), генерируемая колонка
tsvector с русской морфологией и индексы HNSW и GIN, без которых поиск медленный.
По плану это 0002_rag_index; номер 0002 занят настройками салона.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-03 10:00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_VALUES = ("website", "faq", "crm")


def upgrade() -> None:
    op.create_table(
        "knowledge_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "source",
            sa.Enum(*SOURCE_VALUES, name="source", native_enum=False, create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1024), nullable=True),
        sa.Column("embedding_model", sa.String(length=64), nullable=False),
        sa.Column(
            "search",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('russian', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('russian', coalesce(content, '')), 'B')",
                persisted=True,
            ),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["item_id"],
            ["knowledge_items.id"],
            name=op.f("fk_knowledge_chunks_item_id_knowledge_items"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["salon_id"], ["salons.id"], name=op.f("fk_knowledge_chunks_salon_id_salons"), ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint("item_id", "position", name=op.f("uq_knowledge_chunks_item_id_position")),
    )
    op.create_index(op.f("ix_knowledge_chunks_salon_id"), "knowledge_chunks", ["salon_id"], unique=False)
    op.create_index(op.f("ix_knowledge_chunks_item_id"), "knowledge_chunks", ["item_id"], unique=False)
    op.create_index(
        "ix_knowledge_chunks_embedding",
        "knowledge_chunks",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_knowledge_chunks_search", "knowledge_chunks", ["search"], unique=False, postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_search", table_name="knowledge_chunks", postgresql_using="gin")
    op.drop_index("ix_knowledge_chunks_embedding", table_name="knowledge_chunks", postgresql_using="hnsw")
    op.drop_index(op.f("ix_knowledge_chunks_item_id"), table_name="knowledge_chunks")
    op.drop_index(op.f("ix_knowledge_chunks_salon_id"), table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")
