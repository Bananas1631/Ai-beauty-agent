"""Гибридный поиск по базе знаний салона для инструмента salon_info.

Векторный поиск находит синонимы («покраска» ↔ «окрашивание»), полнотекстовый — точные слова
вроде «Shellac»; вместе надёжнее. Слабое совпадение (нет ни слов вопроса, ни близкого смысла)
не отдаётся агенту: лучше «уточню у администратора», чем ответ по чужому фрагменту.
"""

import uuid
from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.repositories.knowledge import Found, KnowledgeRepository
from beauty_agent.domain.enums import KnowledgeSource
from beauty_agent.rag.embeddings import EmbeddingsClient

WEAK_DISTANCE = 0.55  # косинусное расстояние, за которым смысловая близость не в счёт
FRAGMENT_CHARS = 700


def is_weak(found: Found) -> bool:
    return not found.text_match and (found.distance is None or found.distance > WEAK_DISTANCE)


class HybridRetriever:
    def __init__(self, session: AsyncSession, salon_id: uuid.UUID, embedder: EmbeddingsClient) -> None:
        self.repo = KnowledgeRepository(session, salon_id)
        self.embedder = embedder

    async def search(
        self, question: str, *, k: int = 5, sources: Collection[KnowledgeSource] | None = None
    ) -> list[Found]:
        embedded = await self.embedder.embed([question])
        return await self.repo.hybrid_search(
            question, embedded.vectors[0], self.embedder.model_key, k=k, sources=sources
        )


class RagSalonInfo:
    """Источник сведений о салоне для агента: 3 фрагмента со ссылками или ничего."""

    def __init__(self, retriever: HybridRetriever) -> None:
        self.retriever = retriever

    async def search(self, question: str) -> list[dict[str, str]]:
        found = await self.retriever.search(question)
        return fragments(found)


def fragments(found: list[Found], limit: int = 3) -> list[dict[str, str]]:
    """Сильные совпадения — агенту; ссылка — только на страницу сайта, служебный адрес карточки CRM ни к чему."""
    strong = [item for item in found if not is_weak(item)][:limit]
    return [
        {
            "title": item.title,
            "text": item.content[:FRAGMENT_CHARS],
            "url": item.url if item.url and item.url.startswith(("https://", "http://")) else "",
        }
        for item in strong
    ]
