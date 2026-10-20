"""knowledge schema

Схема knowledge индексатора: страницы сайтов и фрагменты с векторами (HNSW) и tsvector (GIN),
очередь обходов, идемпотентность потребителя и outbox событий knowledge.updated.
Расширение vector уже включено миграциями основного сервиса; IF NOT EXISTS — для отдельной базы.

Revision ID: 0001
Create Date: 2026-12-26 10:00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "knowledge"


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint("salon_id", "url", name=op.f("uq_documents_salon_id_url")),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_documents_salon_id"), "documents", ["salon_id"], unique=False, schema=SCHEMA)
    op.create_table(
        "chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=False),
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
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id"],
            [f"{SCHEMA}.documents.id"],
            name=op.f("fk_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("document_id", "position", name=op.f("uq_chunks_document_id_position")),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_chunks_salon_id"), "chunks", ["salon_id"], unique=False, schema=SCHEMA)
    op.create_index(op.f("ix_chunks_document_id"), "chunks", ["document_id"], unique=False, schema=SCHEMA)
    op.create_index(
        "ix_chunks_embedding",
        "chunks",
        ["embedding"],
        unique=False,
        schema=SCHEMA,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.create_index("ix_chunks_search", "chunks", ["search"], unique=False, schema=SCHEMA, postgresql_using="gin")
    op.create_table(
        "reindex_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("site_url", sa.String(length=2048), nullable=False),
        sa.Column("max_pages", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("error", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reindex_jobs")),
        schema=SCHEMA,
    )
    op.create_index(op.f("ix_reindex_jobs_salon_id"), "reindex_jobs", ["salon_id"], unique=False, schema=SCHEMA)
    op.create_index(
        "ix_reindex_jobs_pending",
        "reindex_jobs",
        ["created_at"],
        unique=False,
        schema=SCHEMA,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "processed_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("consumer_group", sa.String(length=100), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_processed_events")),
        sa.UniqueConstraint("consumer_group", "event_id", name=op.f("uq_processed_events_consumer_group_event_id")),
        schema=SCHEMA,
    )
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("topic", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dedupe_key", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event_outbox")),
        sa.UniqueConstraint("event_id", name=op.f("uq_event_outbox_event_id")),
        sa.UniqueConstraint("dedupe_key", name=op.f("uq_event_outbox_dedupe_key")),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_event_outbox_pending",
        "event_outbox",
        ["salon_id", "id"],
        unique=False,
        schema=SCHEMA,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index(op.f("ix_event_outbox_published_at"), "event_outbox", ["published_at"], unique=False, schema=SCHEMA)


def downgrade() -> None:
    op.drop_table("event_outbox", schema=SCHEMA)
    op.drop_table("processed_events", schema=SCHEMA)
    op.drop_table("reindex_jobs", schema=SCHEMA)
    op.drop_table("chunks", schema=SCHEMA)
    op.drop_table("documents", schema=SCHEMA)
