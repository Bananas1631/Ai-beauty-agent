"""Индексация сайта салона: обход страниц, очистка HTML, нарезка, эмбеддинги, сохранение в БД.

- Список страниц — из sitemap.xml (адрес из robots.txt или /sitemap.xml), иначе — ссылки главной
  страницы в пределах того же сайта; не больше 30 страниц, с паузами, с уважением к robots.txt.
- Страница, текст которой не изменился (тот же SHA-256), повторно не индексируется.
- Обход — через безопасный клиент (rag/safe_http.py): внутренние адреса недоступны.
"""

import asyncio
import re
import urllib.robotparser
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.repositories.knowledge import KnowledgeRepository
from beauty_agent.domain.enums import KnowledgeSource
from beauty_agent.rag.chunking import Chunk, chunk_blocks, chunk_page
from beauty_agent.rag.embeddings import EmbeddingsClient
from beauty_agent.rag.extract import Page, extract_links, extract_page
from beauty_agent.rag.safe_http import USER_AGENT, check_url, fetch, make_safe_client, same_site

logger = structlog.get_logger(__name__)

MAX_PAGES = 30
PAUSE_S = 0.5
MIN_PAGE_CHARS = 80  # меньше — пустая или служебная страница
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)
_SKIP_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip", ".mp4", ".doc", ".docx", ".xls")


@dataclass
class IngestReport:
    pages_seen: int = 0
    pages_indexed: int = 0
    pages_unchanged: int = 0
    pages_empty: int = 0
    chunks: int = 0
    tokens: int = 0
    cost_rub: Decimal = Decimal(0)
    errors: list[str] = field(default_factory=list)


async def index_document(
    session: AsyncSession,
    repo: KnowledgeRepository,
    embedder: EmbeddingsClient,
    *,
    source: KnowledgeSource,
    url: str,
    title: str,
    text: str,
    chunks: list[Chunk],
    report: IngestReport,
) -> None:
    item, changed = await repo.upsert_item(source=source, url=url, title=title, content=text)
    if not changed and await repo.chunk_count(item.id, embedder.model_key):
        report.pages_unchanged += 1
        return
    embedded = await embedder.embed([chunk.for_embedding() for chunk in chunks]) if chunks else None
    await repo.replace_chunks(item, chunks, embedded.vectors if embedded else [], embedder.model_key)
    report.pages_indexed += 1
    report.chunks += len(chunks)
    if embedded:
        report.tokens += embedded.tokens
        report.cost_rub += embedded.cost_rub


async def index_text(
    session: AsyncSession,
    salon_id: uuid.UUID,
    embedder: EmbeddingsClient,
    *,
    source: KnowledgeSource,
    url: str,
    title: str,
    text: str,
) -> IngestReport:
    """Индексирует обычный текст: карточку филиала из CRM или FAQ салона."""
    report = IngestReport(pages_seen=1)
    repo = KnowledgeRepository(session, salon_id)
    chunks = chunk_blocks(url, title, text)
    await index_document(
        session, repo, embedder, source=source, url=url, title=title, text=text, chunks=chunks, report=report
    )
    return report


async def _robots(client: httpx.AsyncClient, start_url: str) -> urllib.robotparser.RobotFileParser:
    parser = urllib.robotparser.RobotFileParser()
    fetched = await fetch(client, urljoin(start_url, "/robots.txt"), allowed_types=("text/plain",))
    parser.parse(fetched.text.splitlines() if fetched else [])
    return parser


async def _sitemap_urls(
    client: httpx.AsyncClient, start_url: str, robots: urllib.robotparser.RobotFileParser
) -> list[str]:
    candidates = list(robots.site_maps() or []) or [urljoin(start_url, "/sitemap.xml")]
    urls: list[str] = []
    for sitemap in candidates[:3]:
        if not same_site(start_url, sitemap):
            continue
        fetched = await fetch(client, sitemap, allowed_types=("application/xml", "text/xml"))
        if fetched is None:
            continue
        # Регулярное выражение, а не XML-парсер: недоверенный XML не раскроет сущности (billion laughs)
        urls.extend(url for url in _LOC.findall(fetched.text) if same_site(start_url, url))
    return list(dict.fromkeys(urls))


def _looks_like_page(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return not path.endswith(_SKIP_EXTENSIONS)


async def crawl(
    client: httpx.AsyncClient, start_url: str, *, max_pages: int = MAX_PAGES, pause_s: float = PAUSE_S
) -> AsyncIterator[Page]:
    start = str(check_url(start_url))
    robots = await _robots(client, start)
    queue = await _sitemap_urls(client, start, robots) or [start]
    if start not in queue:
        queue.insert(0, start)
    seen: set[str] = set()
    yielded = 0
    while queue and yielded < max_pages and len(seen) < max_pages * 3:
        url = queue.pop(0)
        if url in seen or not _looks_like_page(url) or not robots.can_fetch(USER_AGENT, url):
            continue
        seen.add(url)
        try:
            fetched = await fetch(client, url)
        except (httpx.HTTPError, ValueError) as exc:
            logger.info("crawl_fetch_failed", url=url, error=type(exc).__name__)
            fetched = None
        if fetched is not None:
            yielded += 1
            yield extract_page(fetched.url, fetched.text)
            for link in extract_links(fetched.url, fetched.text):
                if same_site(start, link) and link not in seen:
                    queue.append(link)
        if pause_s:
            await asyncio.sleep(pause_s)  # вежливый обход: не нагружаем сайт салона


async def ingest_site(
    session: AsyncSession,
    salon_id: uuid.UUID,
    start_url: str,
    embedder: EmbeddingsClient,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = MAX_PAGES,
    pause_s: float = PAUSE_S,
) -> IngestReport:
    report = IngestReport()
    repo = KnowledgeRepository(session, salon_id)
    own_client = client is None
    http = client or make_safe_client()
    try:
        async for page in crawl(http, start_url, max_pages=max_pages, pause_s=pause_s):
            report.pages_seen += 1
            if len(page.text) < MIN_PAGE_CHARS:
                report.pages_empty += 1
                continue
            await index_document(
                session,
                repo,
                embedder,
                source=KnowledgeSource.WEBSITE,
                url=page.url,
                title=page.title,
                text=page.text,
                chunks=chunk_page(page),
                report=report,
            )
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        if own_client:
            await http.aclose()
    logger.info(
        "site_ingested",
        salon_id=str(salon_id),
        pages=report.pages_seen,
        indexed=report.pages_indexed,
        unchanged=report.pages_unchanged,
        chunks=report.chunks,
        cost_rub=str(report.cost_rub),
    )
    return report
