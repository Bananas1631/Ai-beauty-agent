"""Семантический поиск по истории диалогов салона — для администратора.

«Кто спрашивал про наращивание в сентябре»: вопрос превращается в вектор, ищутся ближайшие
сообщения клиентов этого салона за период (векторы считает workers/embeddings.py, только при
согласии клиента). Результат — сообщение, клиент и диалог, чтобы открыть его в инбоксе.

HNSW-индекс общий для всех салонов, а фильтр по салону и периоду pgvector применяет после обхода
индекса: при сотне салонов из 40 ближайших кандидатов своих могло не остаться ни одного.
Итеративный обход (pgvector ≥ 0.8, hnsw.iterative_scan) продолжает поиск, пока не наберёт нужное
число строк, прошедших фильтр, — результат не пустеет от того, что в системе много салонов.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Client, Conversation, Message
from beauty_agent.domain.enums import MessageRole
from beauty_agent.rag.embeddings import EmbeddingsClient

MAX_RESULTS = 50
MIN_SCORE = 0.2  # косинусная близость; ниже — шум, а не «спрашивал про это»
EF_SEARCH = 100


@dataclass(frozen=True, slots=True)
class HistoryHit:
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    client_name: str | None
    content: str
    created_at: datetime
    score: float


class HistorySearch:
    def __init__(self, session: AsyncSession, salon_id: uuid.UUID, embedder: EmbeddingsClient) -> None:
        self.session = session
        self.salon_id = salon_id
        self.embedder = embedder

    async def search(
        self,
        query: str,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 20,
        min_score: float = MIN_SCORE,
    ) -> list[HistoryHit]:
        vector = (await self.embedder.embed([query])).vectors[0]
        distance = Message.embedding.cosine_distance(vector)
        statement = (
            select(
                Message.id,
                Message.conversation_id,
                Client.name,
                Message.content,
                Message.created_at,
                distance.label("distance"),
            )
            .join(Conversation, Conversation.id == Message.conversation_id)
            .join(Client, Client.id == Conversation.client_id)
            .where(
                Message.salon_id == self.salon_id,
                Message.role == MessageRole.CLIENT,
                Message.embedding.is_not(None),
                Message.embedding_model == self.embedder.model_key,  # векторы другой модели несравнимы
            )
            .order_by(distance)
            .limit(min(limit, MAX_RESULTS))
        )
        if since is not None:
            statement = statement.where(Message.created_at >= since)
        if until is not None:
            statement = statement.where(Message.created_at < until)
        # SET LOCAL — только на эту транзакцию: остальные запросы соединения из пула не затронуты
        await self.session.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
        await self.session.execute(text(f"SET LOCAL hnsw.ef_search = {EF_SEARCH}"))
        rows = (await self.session.execute(statement)).all()
        hits = [
            HistoryHit(
                message_id=row.id,
                conversation_id=row.conversation_id,
                client_name=row.name,
                content=row.content,
                created_at=row.created_at,
                score=round(1 - float(row.distance), 4),
            )
            for row in rows
        ]
        # relaxed_order может слегка перемешать порядок — досортировываем уже отобранное
        return sorted((hit for hit in hits if hit.score >= min_score), key=lambda hit: hit.score, reverse=True)
