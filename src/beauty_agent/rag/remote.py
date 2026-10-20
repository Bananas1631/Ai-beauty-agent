"""Сведения о салоне для агента, когда сайт индексирует отдельный сервис (ADR 014, strangler fig).

- IndexerSalonInfo (KNOWLEDGE_BACKEND=indexer): страницы сайта — из индексатора, FAQ и карточка
  филиала — из своего модуля; два списка смешиваются через RRF. Индексатор недоступен — остаются
  FAQ и карточка; если и там пусто, агент говорит «уточню у администратора».
- ShadowSalonInfo (KNOWLEDGE_BACKEND=shadow): ответ агенту — из своего модуля, как раньше, а
  индексатор спрашивается в фоне тем же вопросом; совпадение страниц в топ-3 пишется в метрику и
  лог. По этим цифрам решается переключение на indexer и удаление старого кода обхода сайта.
- KnowledgeVersion: версия базы знаний для ключа кэша ответов — своя часть (FAQ, карточка) плюс
  версия сайта из Redis, которую кладёт потребитель knowledge.updated; нет в Redis — спросить
  индексатор; индексатор недоступен — None, и ответ не кэшируется.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Collection
from typing import Protocol

import structlog
from redis.asyncio import Redis

from beauty_agent.adapters.knowledge_client import SearchResponse
from beauty_agent.core import metrics
from beauty_agent.core.redis import key
from beauty_agent.db.repositories.knowledge import Found
from beauty_agent.domain.enums import KnowledgeSource
from beauty_agent.domain.errors import ProviderUnavailableError
from beauty_agent.rag.fusion import reciprocal_rank_fusion
from beauty_agent.rag.retriever import fragments

logger = structlog.get_logger(__name__)

OWN_SOURCES = (KnowledgeSource.FAQ, KnowledgeSource.CRM)  # сайт — у индексатора
SITE_VERSION_TTL_S = 7 * 24 * 3600
SHADOW_TIMEOUT_S = 5.0
_background: set[asyncio.Task[None]] = set()  # ссылки на фоновые сравнения: иначе их соберёт GC


class SiteSearch(Protocol):
    """Поиск по сайту в индексаторе — KnowledgeClient (в тестах — фейк)."""

    async def search(self, salon_id: uuid.UUID, question: str, *, k: int = 5) -> SearchResponse: ...

    async def version(self, salon_id: uuid.UUID) -> str: ...


class LocalSearch(Protocol):
    """Свой гибридный поиск — HybridRetriever."""

    async def search(
        self, question: str, *, k: int = 5, sources: Collection[KnowledgeSource] | None = None
    ) -> list[Found]: ...


def site_version_key(salon_id: uuid.UUID) -> str:
    return key("knowledge", "site_version", salon_id)


async def remote_found(client: SiteSearch, salon_id: uuid.UUID, question: str, *, k: int = 5) -> list[Found]:
    response = await client.search(salon_id, question, k=k)
    return [
        Found(
            chunk_id=item.chunk_id,
            title=item.title,
            url=item.url,
            content=item.content,
            score=item.score,
            distance=item.distance,
            text_match=item.text_match,
        )
        for item in response.fragments
    ]


def merge(*lists: list[Found], k: int = 5) -> list[Found]:
    """Объединить списки по местам (RRF): оценки индексатора и своего поиска несравнимы."""
    by_id = {item.chunk_id: item for found in lists for item in found}
    fused = reciprocal_rank_fusion([[item.chunk_id for item in found] for found in lists], limit=k)
    return [by_id[chunk_id] for chunk_id, _ in fused]


class IndexerSalonInfo:
    def __init__(self, client: SiteSearch, salon_id: uuid.UUID, local: LocalSearch) -> None:
        self.client = client
        self.salon_id = salon_id
        self.local = local

    async def search(self, question: str) -> list[dict[str, str]]:
        own = await self.local.search(question, sources=OWN_SOURCES)
        try:
            site = await remote_found(self.client, self.salon_id, question)
        except ProviderUnavailableError:
            logger.warning("indexer_unavailable_for_search", salon_id=str(self.salon_id))
            site = []
        return fragments(merge(site, own))


def overlap(local: list[dict[str, str]], remote: list[Found]) -> float:
    """Доля страниц сайта из своего топ-3, которые нашёл и индексатор (1.0 — совпали все)."""
    local_urls = {item["url"] for item in local if item["url"]}
    if not local_urls:
        return 1.0 if not fragments(remote) else 0.0
    remote_urls = {item["url"] for item in fragments(remote) if item["url"]}
    return len(local_urls & remote_urls) / len(local_urls)


class ShadowSalonInfo:
    def __init__(self, client: SiteSearch, salon_id: uuid.UUID, local: LocalSearch) -> None:
        self.client = client
        self.salon_id = salon_id
        self.local = local

    async def search(self, question: str) -> list[dict[str, str]]:
        found = await self.local.search(question)
        result = fragments(found)
        task = asyncio.create_task(self._compare(question, result))
        _background.add(task)
        task.add_done_callback(_background.discard)
        return result

    async def _compare(self, question: str, local: list[dict[str, str]]) -> None:
        try:
            async with asyncio.timeout(SHADOW_TIMEOUT_S):
                remote = await remote_found(self.client, self.salon_id, question)
        except (ProviderUnavailableError, TimeoutError) as exc:
            metrics.knowledge_shadow("error")
            logger.info("knowledge_shadow_failed", salon_id=str(self.salon_id), error=type(exc).__name__)
            return
        share = overlap(local, remote)
        metrics.knowledge_shadow("same" if share == 1.0 else "partial" if share > 0 else "different")
        logger.info("knowledge_shadow_compare", salon_id=str(self.salon_id), overlap=round(share, 2))


async def wait_shadow_comparisons() -> None:
    """Дождаться фоновых сравнений — для тестов и корректной остановки воркера."""
    if _background:
        await asyncio.gather(*list(_background), return_exceptions=True)


class KnowledgeVersion:
    """Версия для ключа кэша ответов: своя часть + версия сайта из индексатора."""

    def __init__(
        self,
        local: Callable[[], Awaitable[str]],
        redis: Redis,
        client: SiteSearch,
        salon_id: uuid.UUID,
    ) -> None:
        self.local = local
        self.redis = redis
        self.client = client
        self.salon_id = salon_id

    async def __call__(self) -> str | None:
        own = await self.local()
        cached = await self.redis.get(site_version_key(self.salon_id))
        site = None if cached is None else cached.decode() if isinstance(cached, bytes) else str(cached)
        if site is None:
            try:
                site = await self.client.version(self.salon_id)
            except ProviderUnavailableError:  # в том числе разомкнутый circuit breaker
                return None  # версия неизвестна — не кэшировать ответ, а не отдать устаревший
            await self.redis.set(site_version_key(self.salon_id), site, ex=SITE_VERSION_TTL_S)
        return f"{own}|site:{site}"
