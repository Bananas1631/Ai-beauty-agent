"""message embeddings

Векторы сообщений клиентов для поиска по истории («кто спрашивал про наращивание в сентябре»):
колонка embedding и модель, которой он посчитан, HNSW-индекс по непустым векторам и частичный
индекс очереди фоновой векторизации. Колонки допускают NULL — добавляются мгновенно, старый код
их не замечает.

Индексы строятся CONCURRENTLY: messages — самая большая таблица, блокировать запись в неё на время
построения HNSW нельзя. Если построение прервалось, остаётся невалидный индекс — удалить его
DROP INDEX CONCURRENTLY и запустить миграцию снова (IF NOT EXISTS его не пересоздаст).

Revision ID: 0009
Create Date: 2026-12-19 10:00:00
"""

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("embedding", pgvector.sqlalchemy.Vector(dim=1024), nullable=True))
    op.add_column("messages", sa.Column("embedding_model", sa.String(length=32), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_messages_embedding",
            "messages",
            ["embedding"],
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_where=sa.text("embedding IS NOT NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )
        op.create_index(
            "ix_messages_pending_embedding",
            "messages",
            ["created_at"],
            postgresql_where=sa.text("role = 'client' AND embedding IS NULL"),
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index("ix_messages_pending_embedding", "messages", postgresql_concurrently=True, if_exists=True)
        op.drop_index("ix_messages_embedding", "messages", postgresql_concurrently=True, if_exists=True)
    op.drop_column("messages", "embedding_model")
    op.drop_column("messages", "embedding")
