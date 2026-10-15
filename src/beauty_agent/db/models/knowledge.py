"""База знаний салона: документы (страницы сайта, FAQ, карточка филиала) и их фрагменты с векторами."""

import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Computed, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from beauty_agent.db.base import Base, SalonOwned, Timestamps, UUIDPrimaryKey, str_enum
from beauty_agent.domain.enums import KnowledgeSource

EMBEDDING_DIMENSIONS = 1024  # bge-m3 и GigaChat Embeddings; совпадает с rag/embeddings.py


class KnowledgeItem(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Документ базы знаний, по которому агент отвечает на общие вопросы."""

    __tablename__ = "knowledge_items"
    __table_args__ = (UniqueConstraint("salon_id", "url"),)  # страница сайта — один документ

    source: Mapped[KnowledgeSource] = mapped_column(str_enum(KnowledgeSource, "source"))
    url: Mapped[str | None] = mapped_column(String(2048))  # у FAQ адреса нет
    title: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    # SHA-256 текста: страницу, которая не изменилась, повторно не индексируем
    content_hash: Mapped[str] = mapped_column(String(64))


class KnowledgeChunk(UUIDPrimaryKey, SalonOwned, Timestamps, Base):
    """Фрагмент документа на 300–500 токенов: вектор для смыслового поиска и tsvector — для точных слов."""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("item_id", "position"),
        # HNSW — приближённый поиск ближайших векторов по косинусному расстоянию за миллисекунды
        Index(
            "ix_knowledge_chunks_embedding",
            "embedding",
            postgresql_using="hnsw",
            postgresql_with={"m": 16, "ef_construction": 64},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        # GIN по tsvector — полнотекстовый поиск: точные слова вроде «Shellac» и «Kerastase»
        Index("ix_knowledge_chunks_search", "search", postgresql_using="gin"),
    )

    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("knowledge_items.id", ondelete="CASCADE"), index=True)
    position: Mapped[int]
    source: Mapped[KnowledgeSource] = mapped_column(str_enum(KnowledgeSource, "source"))
    url: Mapped[str | None] = mapped_column(String(2048))
    title: Mapped[str] = mapped_column(String(500))  # «Страница — раздел»
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIMENSIONS))
    # Векторы разных моделей несравнимы: поиск сравнивает вопрос только с векторами той же модели
    embedding_model: Mapped[str] = mapped_column(String(64))
    # Генерирует сама БД: заголовок весит больше текста; русская морфология — «стрижки» найдёт «стрижка»
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('russian', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('russian', coalesce(content, '')), 'B')",
            persisted=True,
        ),
    )
