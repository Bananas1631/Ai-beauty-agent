"""Evals поиска по базе знаний: recall@5 и MRR на 30 вопросах по трём вымышленным сайтам.

    poetry run python -m evals.rag_run                      # офлайн-эмбеддинги (хеширование)
    poetry run python -m evals.rag_run --embedder bge-m3     # облачные, нужен LLM_CLOUDRU_API_KEY

recall@5 — доля вопросов, для которых нужный фрагмент (страница + фраза) попал в пятёрку лучших.
Нужен PostgreSQL с pgvector (make up). Прогон идёт в транзакции и откатывается: в базе ничего не остаётся.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.core.config import PostgresSettings, get_settings
from beauty_agent.core.db import make_engine
from beauty_agent.core.logging import configure_logging
from beauty_agent.db.models import Salon
from beauty_agent.db.repositories.knowledge import Found
from beauty_agent.rag.embeddings import EmbeddingsClient, HashingEmbeddings
from beauty_agent.rag.ingest import ingest_site
from beauty_agent.rag.retriever import HybridRetriever
from evals.metrics import normalize_numbers
from evals.site_server import site_client, site_host

DATASET = Path(__file__).parent / "datasets" / "rag" / "v1.jsonl"
REPORTS = Path(__file__).parent / "reports"
K = 5


class RagExpect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: str  # "" — главная страница
    phrase: str


class RagQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    site: str
    question: str
    expect: RagExpect


def load_questions(path: Path = DATASET) -> list[RagQuestion]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [RagQuestion.model_validate_json(line) for line in lines if line.strip()]


def _norm(text: str) -> str:
    return normalize_numbers(text).lower().replace("ё", "е")


def hit_rank(question: RagQuestion, found: Sequence[Found]) -> int | None:
    """Место (с 1) первого подходящего фрагмента или None."""
    wanted_url = f"https://{site_host(question.site)}/{question.expect.page}"
    phrase = _norm(question.expect.phrase)
    for rank, item in enumerate(found, start=1):
        if item.url == wanted_url and phrase in _norm(item.content):
            return rank
    return None


async def run_rag(
    session: AsyncSession, embedder: EmbeddingsClient, questions: Sequence[RagQuestion]
) -> dict[str, Any]:
    salons: dict[str, uuid.UUID] = {}
    for site in sorted({question.site for question in questions}):
        salon = Salon(slug=f"rag-eval-{site}-{uuid.uuid4().hex[:6]}", name=f"RAG eval {site}")
        session.add(salon)
        await session.flush()
        salons[site] = salon.id
        async with site_client(site) as client:
            await ingest_site(session, salon.id, f"https://{site_host(site)}/", embedder, client=client, pause_s=0)

    results: list[dict[str, Any]] = []
    for question in questions:
        found = await HybridRetriever(session, salons[question.site], embedder).search(question.question, k=K)
        rank = hit_rank(question, found)
        results.append(
            {
                "id": question.id,
                "question": question.question,
                "rank": rank,
                "top": [{"url": item.url, "title": item.title, "score": round(item.score, 5)} for item in found],
            }
        )
    hits = [result["rank"] for result in results if result["rank"] is not None]
    return {
        "embedder": embedder.model_key,
        "questions": len(results),
        "recall_at_5": round(len(hits) / len(results), 4) if results else 0.0,
        "mrr": round(sum(1 / rank for rank in hits) / len(results), 4) if results else 0.0,
        "results": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Evals RAG: rag/v1",
        "",
        f"Прогон {report['started_at']}, эмбеддинги: `{report['embedder']}`.",
        "Гибридный поиск: вектор + полнотекст, объединение RRF.",
        "",
        "| Вопросов | recall@5 | MRR |",
        "|---|---|---|",
        f"| {report['questions']} | {report['recall_at_5']:.0%} | {report['mrr']:.2f} |",
    ]
    missed = [result for result in report["results"] if result["rank"] is None]
    if missed:
        lines += ["", f"## Не найдено в пятёрке ({len(missed)})", ""]
        lines += [f"- `{item['id']}`: {item['question']}" for item in missed]
    return "\n".join(lines) + "\n"


async def main_async(args: argparse.Namespace, postgres: PostgresSettings) -> dict[str, Any]:
    embedder = HashingEmbeddings() if args.embedder == "hashing" else build_embedder(get_settings().llm)
    engine = make_engine(postgres)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False)
            try:
                report = await run_rag(session, embedder, load_questions())
            finally:
                await session.close()
                await transaction.rollback()  # данные прогона в базе не остаются
    finally:
        await engine.dispose()
    report["started_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evals поиска по базе знаний")
    parser.add_argument("--embedder", choices=["hashing", "bge-m3"], default="hashing")
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)
    configure_logging("WARNING", json_logs=False)
    report = asyncio.run(main_async(args, get_settings().postgres))
    print(f"recall@5 = {report['recall_at_5']:.0%}, MRR = {report['mrr']:.2f} ({report['embedder']})")
    if not args.no_report:
        REPORTS.mkdir(parents=True, exist_ok=True)
        stem = f"{report['started_at'][:10]}-rag-v1-{report['embedder']}"
        (REPORTS / f"{stem}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        (REPORTS / f"{stem}.md").write_text(render_markdown(report), encoding="utf-8", newline="\n")
        print(f"Отчёт: {REPORTS / stem}.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
