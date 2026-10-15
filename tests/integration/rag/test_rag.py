"""RAG на настоящем PostgreSQL с pgvector: индексация вымышленного сайта и гибридный поиск."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import KnowledgeChunk, Salon
from beauty_agent.rag.embeddings import HashingEmbeddings
from beauty_agent.rag.ingest import ingest_site
from beauty_agent.rag.retriever import HybridRetriever, RagSalonInfo, is_weak
from evals.rag_run import load_questions, run_rag
from evals.site_server import site_client, site_host

EMBEDDER = HashingEmbeddings()
LAVANDA = f"https://{site_host('lavanda')}/"


async def ingest(session: AsyncSession, salon: Salon, site: str = "lavanda", *, sitemap: bool = True) -> object:
    async with site_client(site, with_sitemap=sitemap) as client:
        return await ingest_site(session, salon.id, f"https://{site_host(site)}/", EMBEDDER, client=client, pause_s=0)


async def chunks_of(session: AsyncSession, salon: Salon) -> int:
    statement = select(func.count()).select_from(KnowledgeChunk).where(KnowledgeChunk.salon_id == salon.id)
    return int((await session.execute(statement)).scalar_one())


async def test_site_is_indexed(session: AsyncSession, salon: Salon) -> None:
    report = await ingest(session, salon)

    assert report.pages_seen == 5  # type: ignore[attr-defined]
    assert report.pages_indexed == 5  # type: ignore[attr-defined]
    stored = (await session.scalars(select(KnowledgeChunk).where(KnowledgeChunk.salon_id == salon.id))).all()
    assert stored
    assert all(chunk.embedding_model == "hashing" for chunk in stored)
    assert not any("Политика конфиденциальности" in chunk.content for chunk in stored)  # подвал вырезан
    assert not any("Перезвоните" in chunk.content for chunk in stored)  # форма вырезана


async def test_links_are_followed_without_sitemap(session: AsyncSession, salon: Salon) -> None:
    report = await ingest(session, salon, sitemap=False)

    assert report.pages_indexed == 5  # type: ignore[attr-defined]


async def test_unchanged_pages_are_skipped(session: AsyncSession, salon: Salon) -> None:
    await ingest(session, salon)
    before = await chunks_of(session, salon)

    again = await ingest(session, salon)

    assert again.pages_unchanged == 5  # type: ignore[attr-defined]
    assert again.pages_indexed == 0  # type: ignore[attr-defined]
    assert await chunks_of(session, salon) == before


async def test_hybrid_search_finds_exact_words(session: AsyncSession, salon: Salon) -> None:
    await ingest(session, salon)

    found = await HybridRetriever(session, salon.id, EMBEDDER).search("Сколько стоит Shellac?")

    # «Shellac» есть и в ценах, и в уходе; без смысловой модели «стоит» не тянет к ценам — обе в начале списка
    assert f"{LAVANDA}prices.html" in [item.url for item in found[:3]]
    assert found[0].text_match
    assert "Shellac" in found[0].content
    assert len(found) <= 5


async def test_search_is_isolated_by_salon(session: AsyncSession, salon: Salon, other_salon: Salon) -> None:
    await ingest(session, salon)
    await ingest(session, other_salon, "boroda")

    found = await HybridRetriever(session, other_salon.id, EMBEDDER).search("Сколько стоит Shellac?")

    assert all(site_host("boroda") in (item.url or "") for item in found)


async def test_weak_match_is_not_given_to_agent(session: AsyncSession, salon: Salon) -> None:
    await ingest(session, salon)
    info = RagSalonInfo(HybridRetriever(session, salon.id, EMBEDDER))

    strong = await info.search("Можно ли оплатить по СБП?")
    weak = await info.search("квантовая хромодинамика")

    assert strong
    assert strong[0]["url"] == f"{LAVANDA}contacts.html"
    assert weak == []


async def test_vectors_of_other_model_are_ignored(session: AsyncSession, salon: Salon) -> None:
    await ingest(session, salon)

    class OtherModel(HashingEmbeddings):
        model_key = "other"

    found = await HybridRetriever(session, salon.id, OtherModel()).search("квантовая хромодинамика")

    assert found == []  # векторы другой модели не сравниваются, а слов вопроса на сайте нет
    assert all(is_weak(item) for item in found)


@pytest.mark.parametrize("site", ["lavanda", "boroda", "iris"])
async def test_every_site_page_is_reachable(session: AsyncSession, salon: Salon, site: str) -> None:
    report = await ingest(session, salon, site)

    assert report.pages_indexed >= 4  # type: ignore[attr-defined]


async def test_rag_eval_recall(session: AsyncSession) -> None:
    report = await run_rag(session, EMBEDDER, load_questions())

    assert report["questions"] == 30
    # Нижняя планка на офлайн-эмбеддингах: полнотекстовый поиск вытягивает точные слова
    assert report["recall_at_5"] >= 0.8, [r for r in report["results"] if r["rank"] is None]
