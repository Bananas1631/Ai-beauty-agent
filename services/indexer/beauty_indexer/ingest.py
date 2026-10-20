"""Обход сайта салона и индексация страниц в схему knowledge.

Обходчик, очистка HTML, нарезка и эмбеддинги — общая библиотека beauty_agent.rag (тот же код, что
работал в основном сервисе до выделения индексатора): поведение не меняется, меняется место, где
оно выполняется, и хранилище. Каждая страница сохраняется своей транзакцией — повтор после сбоя
продолжает с упавшей страницы, неизменённые страницы пропускаются по SHA-256.
"""

import uuid

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from beauty_agent.rag.chunking import chunk_page
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.rag.ingest import MIN_PAGE_CHARS, IngestReport, crawl
from beauty_agent.rag.safe_http import make_safe_client
from beauty_indexer.store import KnowledgeStore

logger = structlog.get_logger(__name__)


async def index_site(
    session_factory: async_sessionmaker[AsyncSession],
    salon_id: uuid.UUID,
    site_url: str,
    embedder: EmbeddingsClient,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = 30,
    pause_s: float = 0.5,
) -> IngestReport:
    report = IngestReport()
    own_client = client is None
    http = client or make_safe_client()
    try:
        async for page in crawl(http, site_url, max_pages=max_pages, pause_s=pause_s):
            report.pages_seen += 1
            if len(page.text) < MIN_PAGE_CHARS:
                report.pages_empty += 1
                continue
            async with session_factory() as session, session.begin():
                store = KnowledgeStore(session, salon_id)
                document, changed = await store.upsert_document(url=page.url, title=page.title, content=page.text)
                if not changed and await store.chunk_count(document.id, embedder.model_key):
                    report.pages_unchanged += 1
                    continue
                chunks = chunk_page(page)
                embedded = await embedder.embed([chunk.for_embedding() for chunk in chunks]) if chunks else None
                await store.replace_chunks(document, chunks, embedded.vectors if embedded else [], embedder.model_key)
            report.pages_indexed += 1
            report.chunks += len(chunks)
            if embedded:
                report.tokens += embedded.tokens
                report.cost_rub += embedded.cost_rub
    finally:
        if own_client:
            await http.aclose()
    logger.info(
        "site_indexed",
        salon_id=str(salon_id),
        pages=report.pages_seen,
        indexed=report.pages_indexed,
        unchanged=report.pages_unchanged,
        chunks=report.chunks,
        cost_rub=str(report.cost_rub),
    )
    return report
