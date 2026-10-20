"""Таблицы индексатора — в своей схеме knowledge, со своими миграциями.

Основной сервис в эти таблицы не ходит: только через REST API индексатора. Внешних ключей на
таблицы основного сервиса (salons) нет — граница сервиса проходит и через схему: salon_id здесь
просто идентификатор из события.

- documents и chunks — то же, что knowledge_items и knowledge_chunks основного сервиса, но только
  страницы сайтов (FAQ и карточка филиала остаются в основном сервисе);
- reindex_jobs — очередь обходов: событие из Kafka превращается в строку быстро, а обход (минуты)
  идёт отдельным циклом, не задерживая потребителя (иначе Kafka сочла бы его зависшим);
- processed_events и event_outbox — идемпотентность потребителя и transactional outbox событий
  knowledge.updated, как в основном сервисе.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, DateTime, ForeignKey, Index, MetaData, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from beauty_agent.db.base import NAMING_CONVENTION, uuid7
from beauty_agent.db.models.event_outbox import PENDING, OutboxColumns
from beauty_agent.db.models.knowledge import EMBEDDING_DIMENSIONS

SCHEMA = "knowledge"


def include_name(name: str | None, type_: str, _parent_names: object) -> bool:
    """Alembic autogenerate смотрит только на свою схему: таблицы основного сервиса — не наши."""
    return name == SCHEMA if type_ == "schema" else True


class Base(DeclarativeBase):
    metadata = MetaData(schema=SCHEMA, naming_convention=NAMING_CONVENTION)
    type_annotation_map = {datetime: DateTime(timezone=True)}  # noqa: RUF012
    __mapper_args__ = {"eager_defaults": True}  # noqa: RUF012


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Document(Base):
    """Страница сайта салона: текст целиком и его SHA-256 — неизменённая страница не индексируется."""

    __tablename__ = "documents"
    __table_args__ = (UniqueConstraint("salon_id", "url"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    salon_id: Mapped[uuid.UUID] = mapped_column(index=True)
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class Chunk(Base):
    """Фрагмент страницы: вектор для смыслового поиска и tsvector — для точных слов."""

    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("document_id", "position"),
        Index(
            "ix_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_search", "search", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    salon_id: Mapped[uuid.UUID] = mapped_column(index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey(f"{SCHEMA}.documents.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    url: Mapped[str] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    embedding_model: Mapped[str] = mapped_column(String(64))
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('russian', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('russian', coalesce(content, '')), 'B')",
            persisted=True,
        ),
    )
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class ReindexJob(Base):
    __tablename__ = "reindex_jobs"
    __table_args__ = (
        # очередь обходов: ждущие задачи по порядку поступления
        Index("ix_reindex_jobs_pending", "created_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    salon_id: Mapped[uuid.UUID] = mapped_column(index=True)
    site_url: Mapped[str] = mapped_column(String(2048))
    max_pages: Mapped[int]
    job_id: Mapped[uuid.UUID | None]  # задача JobStore основного сервиса — вернётся в knowledge.updated
    status: Mapped[str] = mapped_column(String(16), server_default=JobStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(server_default="0")
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class ProcessedEvent(Base):
    __tablename__ = "processed_events"
    __table_args__ = (UniqueConstraint("consumer_group", "event_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid7)
    salon_id: Mapped[uuid.UUID]
    consumer_group: Mapped[str] = mapped_column(String(100))
    event_id: Mapped[uuid.UUID]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class EventOutbox(OutboxColumns, Base):
    """knowledge.updated пишется сюда в одной транзакции с завершением задачи обхода."""

    __tablename__ = "event_outbox"
    __table_args__ = (
        Index("ix_event_outbox_pending", "salon_id", "id", postgresql_where=PENDING),
        Index(None, "published_at"),
    )
