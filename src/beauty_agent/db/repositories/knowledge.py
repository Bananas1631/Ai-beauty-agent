"""Документы и фрагменты базы знаний: сохранение и гибридный поиск.

Поиск — два списка кандидатов в одном SQL-запросе: векторный топ-20 (близость по смыслу)
и полнотекстовый топ-20 (точные слова), оба только по своему салону. Списки объединяются
через Reciprocal Rank Fusion: score = Σ 1 / (60 + место в списке). RRF не требует сводить
несравнимые оценки (косинус и ts_rank) к одной шкале — важен только порядок.
"""

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import Text, cast, delete, func, literal, or_, select, text
from sqlalchemy.dialects.postgresql import TSQUERY, insert

from beauty_agent.db.models import KnowledgeChunk, KnowledgeItem
from beauty_agent.db.repositories.base import SalonRepository
from beauty_agent.domain.enums import KnowledgeSource
from beauty_agent.rag.chunking import Chunk

CANDIDATES = 20
RRF_K = 60


@dataclass(frozen=True, slots=True)
class Found:
    chunk_id: uuid.UUID
    title: str
    url: str | None
    content: str
    score: float
    distance: float | None  # косинусное расстояние: 0 — тот же смысл, 1 — несвязанный текст
    text_match: bool  # нашёлся ли полнотекстовым поиском


def content_hash(text_value: str) -> str:
    return hashlib.sha256(text_value.encode()).hexdigest()


class KnowledgeRepository(SalonRepository[KnowledgeItem]):
    model = KnowledgeItem

    async def by_url(self, url: str) -> KnowledgeItem | None:
        return await self.session.scalar(self._select().filter_by(url=url))

    async def upsert_item(
        self, *, source: KnowledgeSource, url: str, title: str, content: str
    ) -> tuple[KnowledgeItem, bool]:
        """Создаёт или обновляет документ; второе значение — изменился ли текст."""
        digest = content_hash(content)
        existing = await self.by_url(url)
        if existing is not None and existing.content_hash == digest:
            return existing, False
        values = {"source": source, "title": title[:500], "content": content, "content_hash": digest}
        statement = (
            insert(KnowledgeItem)
            .values(salon_id=self.salon_id, url=url, **values)
            .on_conflict_do_update(index_elements=["salon_id", "url"], set_=values)
            .returning(KnowledgeItem.id)
        )
        item_id = (await self.session.execute(statement)).scalar_one()
        item = await self.session.get(KnowledgeItem, item_id, populate_existing=True)
        if item is None:  # строку только что вставили — её не может не быть
            raise RuntimeError("knowledge item vanished")
        return item, True

    async def chunk_count(self, item_id: uuid.UUID, embedding_model: str) -> int:
        """Сколько фрагментов документа посчитано этой моделью: другая модель — индексировать заново."""
        statement = (
            select(func.count())
            .select_from(KnowledgeChunk)
            .where(
                KnowledgeChunk.salon_id == self.salon_id,
                KnowledgeChunk.item_id == item_id,
                KnowledgeChunk.embedding_model == embedding_model,
            )
        )
        return int((await self.session.execute(statement)).scalar_one())

    async def replace_chunks(
        self,
        item: KnowledgeItem,
        chunks: Sequence[Chunk],
        vectors: Sequence[list[float] | None],
        embedding_model: str,
    ) -> None:
        await self.session.execute(
            delete(KnowledgeChunk).where(KnowledgeChunk.salon_id == self.salon_id, KnowledgeChunk.item_id == item.id)
        )
        for chunk, vector in zip(chunks, vectors, strict=True):
            title = " — ".join(part for part in (chunk.page_title, chunk.section) if part)
            self.session.add(
                KnowledgeChunk(
                    salon_id=self.salon_id,
                    item_id=item.id,
                    position=chunk.position,
                    source=item.source,
                    url=item.url,
                    title=title[:500],
                    content=chunk.text,
                    embedding=vector,
                    embedding_model=embedding_model,
                )
            )
        await self.session.flush()

    async def hybrid_search(
        self, question: str, vector: list[float], embedding_model: str, *, k: int = 5
    ) -> list[Found]:
        # Фильтр по салону применяется к результатам HNSW: итеративный обход индекса (pgvector 0.8)
        # добирает кандидатов, пока не наберётся 20 своих, а не отдаёт 20 общих с отсевом чужих
        await self.session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        own = KnowledgeChunk.salon_id == self.salon_id

        distance = KnowledgeChunk.embedding.cosine_distance(vector)
        nearest = (
            select(KnowledgeChunk.id.label("id"), distance.label("distance"))
            .where(own, KnowledgeChunk.embedding_model == embedding_model, KnowledgeChunk.embedding.is_not(None))
            .order_by(distance)
            .limit(CANDIDATES)
            .subquery()
        )
        vec = select(
            nearest.c.id, nearest.c.distance, func.row_number().over(order_by=nearest.c.distance).label("rank")
        ).cte("vec")

        # Слова вопроса через ИЛИ: «Сколько стоит Shellac?» должно находить фрагмент со словом Shellac,
        # даже если там нет «сколько» и «стоит». ts_rank_cd ставит выше фрагменты с большим числом слов.
        # plainto_tsquery нормализует слова (русская морфология) и экранирует ввод — это не SQL, а данные
        conjunction = cast(func.plainto_tsquery("russian", question), Text)
        query = cast(func.replace(conjunction, " & ", " | "), TSQUERY)
        rank = func.ts_rank_cd(KnowledgeChunk.search, query)
        matched = (
            select(KnowledgeChunk.id.label("id"), rank.label("text_rank"))
            .where(own, KnowledgeChunk.search.op("@@")(query))
            .order_by(rank.desc())
            .limit(CANDIDATES)
            .subquery()
        )
        fts = select(matched.c.id, func.row_number().over(order_by=matched.c.text_rank.desc()).label("rank")).cte("fts")

        score = func.coalesce(literal(1.0) / (RRF_K + vec.c.rank), 0) + func.coalesce(
            literal(1.0) / (RRF_K + fts.c.rank), 0
        )
        statement = (
            select(KnowledgeChunk, score.label("score"), vec.c.distance, fts.c.rank.is_not(None).label("text_match"))
            .outerjoin(vec, vec.c.id == KnowledgeChunk.id)
            .outerjoin(fts, fts.c.id == KnowledgeChunk.id)
            .where(own, or_(vec.c.id.is_not(None), fts.c.id.is_not(None)))
            .order_by(score.desc(), KnowledgeChunk.id)
            .limit(k)
        )
        rows = (await self.session.execute(statement)).all()
        return [
            Found(
                chunk_id=chunk.id,
                title=chunk.title,
                url=chunk.url,
                content=chunk.content,
                score=float(row_score),
                distance=None if row_distance is None else float(row_distance),
                text_match=bool(row_match),
            )
            for chunk, row_score, row_distance, row_match in rows
        ]
