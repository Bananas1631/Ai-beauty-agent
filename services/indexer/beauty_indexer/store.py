"""Хранилище индексатора: страницы, фрагменты и гибридный поиск в пределах одного салона.

Поиск — два отдельных запроса кандидатов (векторный топ-20 и полнотекстовый топ-20) и их слияние
через RRF в Python (beauty_agent/rag/fusion.py). В основном сервисе то же делает один SQL-запрос;
здесь проще читать, тестировать и замерять каждую часть, а цена — лишний запрос в пределах той же
транзакции (~1 мс на кандидатах одного салона).
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Text, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY, insert
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.base import uuid7
from beauty_agent.rag.chunking import Chunk as TextChunk
from beauty_agent.rag.fusion import reciprocal_rank_fusion
from beauty_indexer.db import Chunk, Document

CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class Fragment:
    chunk_id: uuid.UUID
    title: str
    url: str
    content: str
    score: float
    distance: float | None
    text_match: bool


def content_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class KnowledgeStore:
    def __init__(self, session: AsyncSession, salon_id: uuid.UUID) -> None:
        self.session = session
        self.salon_id = salon_id

    async def upsert_document(self, *, url: str, title: str, content: str) -> tuple[Document, bool]:
        """Создаёт или обновляет страницу; второе значение — изменился ли текст."""
        digest = content_hash(content)
        existing = await self.session.scalar(
            select(Document).where(Document.salon_id == self.salon_id, Document.url == url)
        )
        if existing is not None and existing.content_hash == digest:
            return existing, False
        values = {"title": title[:500], "content": content, "content_hash": digest, "updated_at": func.now()}
        statement = (
            insert(Document)
            .values(id=uuid7(), salon_id=self.salon_id, url=url, **values)
            .on_conflict_do_update(index_elements=["salon_id", "url"], set_=values)
            .returning(Document.id)
        )
        document_id = (await self.session.execute(statement)).scalar_one()
        document = await self.session.get(Document, document_id, populate_existing=True)
        if document is None:
            raise RuntimeError("document vanished")
        return document, True

    async def chunk_count(self, document_id: uuid.UUID, embedding_model: str) -> int:
        statement = select(func.count()).where(
            Chunk.salon_id == self.salon_id, Chunk.document_id == document_id, Chunk.embedding_model == embedding_model
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def replace_chunks(
        self,
        document: Document,
        chunks: Sequence[TextChunk],
        vectors: Sequence[list[float] | None],
        embedding_model: str,
    ) -> None:
        await self.session.execute(
            delete(Chunk).where(Chunk.salon_id == self.salon_id, Chunk.document_id == document.id)
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            title = " — ".join(part for part in (chunk.page_title, chunk.section) if part)
            self.session.add(
                Chunk(
                    salon_id=self.salon_id,
                    document_id=document.id,
                    position=chunk.position,
                    url=document.url,
                    title=title[:500],
                    content=chunk.text,
                    embedding=vector,
                    embedding_model=embedding_model,
                )
            )
        await self.session.flush()

    async def version(self) -> str:
        """Число фрагментов и время последнего: меняется с каждой переиндексацией изменённых страниц."""
        statement = select(func.count(), func.max(Chunk.created_at)).where(Chunk.salon_id == self.salon_id)
        count, latest = (await self.session.execute(statement)).one()
        return f"{count}:{latest.isoformat() if latest else '-'}"

    # ------------------------------------------------------------- поиск

    async def vector_candidates(self, vector: list[float], embedding_model: str) -> list[tuple[uuid.UUID, float]]:
        await self.session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        distance = Chunk.embedding.cosine_distance(vector)
        rows = await self.session.execute(
            select(Chunk.id, distance)
            .where(
                Chunk.salon_id == self.salon_id,
                Chunk.embedding_model == embedding_model,
                Chunk.embedding.is_not(None),
            )
            .order_by(distance)
            .limit(CANDIDATES)
        )
        return [(row[0], float(row[1])) for row in rows]

    async def text_candidates(self, question: str) -> list[uuid.UUID]:
        # Слова вопроса через ИЛИ (как в основном сервисе): plainto_tsquery экранирует ввод
        conjunction = cast(func.plainto_tsquery("russian", question), Text)
        query = cast(func.replace(conjunction, " & ", " | "), TSQUERY)
        rank = func.ts_rank_cd(Chunk.search, query)
        rows = await self.session.scalars(
            select(Chunk.id)
            .where(Chunk.salon_id == self.salon_id, Chunk.search.op("@@")(query))
            .order_by(rank.desc(), Chunk.id)
            .limit(CANDIDATES)
        )
        return list(rows)

    async def hybrid_search(
        self, question: str, vector: list[float], embedding_model: str, *, k: int = 5
    ) -> list[Fragment]:
        nearest = await self.vector_candidates(vector, embedding_model)
        matched = await self.text_candidates(question)
        fused = reciprocal_rank_fusion([[chunk_id for chunk_id, _ in nearest], matched], limit=k)
        if not fused:
            return []
        distances = dict(nearest)
        text_hits = set(matched)
        chunks = {
            chunk.id: chunk
            for chunk in await self.session.scalars(
                select(Chunk).where(Chunk.salon_id == self.salon_id, Chunk.id.in_([chunk_id for chunk_id, _ in fused]))
            )
        }
        return [
            Fragment(
                chunk_id=chunk_id,
                title=chunks[chunk_id].title,
                url=chunks[chunk_id].url,
                content=chunks[chunk_id].content,
                score=score,
                distance=distances.get(chunk_id),
                text_match=chunk_id in text_hits,
            )
            for chunk_id, score in fused
            if chunk_id in chunks
        ]
