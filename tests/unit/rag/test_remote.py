"""Сведения о сайте из индексатора: смешивание с FAQ, недоступный индексатор, тень и версия для кэша."""

import uuid
from collections.abc import AsyncIterator, Collection

import pytest
from fakeredis import FakeAsyncRedis
from prometheus_client import REGISTRY

from beauty_agent.adapters.knowledge_client import (
    FragmentOut,
    KnowledgeUnavailableError,
    SearchResponse,
)
from beauty_agent.db.repositories.knowledge import Found
from beauty_agent.domain.enums import KnowledgeSource
from beauty_agent.rag.remote import (
    IndexerSalonInfo,
    KnowledgeVersion,
    ShadowSalonInfo,
    overlap,
    site_version_key,
    wait_shadow_comparisons,
)

SALON = uuid.uuid4()


def found(title: str, url: str | None, *, text_match: bool = True) -> Found:
    return Found(uuid.uuid4(), title, url, f"{title}: текст", 0.03, 0.2, text_match)


class FakeIndexer:
    def __init__(self, items: list[Found] | None = None, *, down: bool = False, version: str = "7:t") -> None:
        self.items = items or []
        self.down = down
        self.site_version = version
        self.searches = 0
        self.versions = 0

    async def search(self, salon_id: uuid.UUID, question: str, *, k: int = 5) -> SearchResponse:
        self.searches += 1
        if self.down:
            raise KnowledgeUnavailableError
        return SearchResponse(
            version=self.site_version,
            fragments=[
                FragmentOut(
                    chunk_id=item.chunk_id,
                    title=item.title,
                    url=item.url,
                    content=item.content,
                    score=item.score,
                    distance=item.distance,
                    text_match=item.text_match,
                )
                for item in self.items
            ],
        )

    async def version(self, salon_id: uuid.UUID) -> str:
        self.versions += 1
        if self.down:
            raise KnowledgeUnavailableError
        return self.site_version


class FakeLocal:
    def __init__(self, items: list[Found]) -> None:
        self.items = items
        self.sources: list[Collection[KnowledgeSource] | None] = []

    async def search(
        self, question: str, *, k: int = 5, sources: Collection[KnowledgeSource] | None = None
    ) -> list[Found]:
        self.sources.append(sources)
        return self.items


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()


def shadow_count(result: str) -> float:
    return REGISTRY.get_sample_value("beauty_agent_knowledge_shadow_total", {"result": result}) or 0.0


async def test_site_from_indexer_and_faq_from_own_module() -> None:
    faq = found("Парковка", "faq:abc")
    site = found("Цены", "https://lavanda.example/prices.html")
    local = FakeLocal([faq])
    info = IndexerSalonInfo(FakeIndexer([site]), SALON, local)

    result = await info.search("Где парковаться и сколько стоит маникюр?")

    assert {item["title"] for item in result} == {"Парковка", "Цены"}
    assert local.sources == [(KnowledgeSource.FAQ, KnowledgeSource.CRM)]  # сайт свой модуль не ищет
    assert next(item for item in result if item["title"] == "Парковка")["url"] == ""  # служебный адрес не наружу


async def test_indexer_down_leaves_faq_only() -> None:
    info = IndexerSalonInfo(FakeIndexer(down=True), SALON, FakeLocal([found("Оплата", None)]))

    result = await info.search("Можно картой?")

    assert [item["title"] for item in result] == ["Оплата"]


async def test_indexer_down_and_no_faq_means_ask_admin() -> None:
    info = IndexerSalonInfo(FakeIndexer(down=True), SALON, FakeLocal([]))

    assert await info.search("Есть ли парковка?") == []  # агент скажет «уточню у администратора»


async def test_shadow_answers_from_own_module_and_compares_in_background() -> None:
    page = found("Цены", "https://lavanda.example/prices.html")
    other = found("Уход", "https://lavanda.example/care.html")
    before = shadow_count("partial"), shadow_count("same")
    indexer = FakeIndexer([page])
    info = ShadowSalonInfo(indexer, SALON, FakeLocal([page, other]))

    result = await info.search("цены")
    await wait_shadow_comparisons()

    assert [item["title"] for item in result] == ["Цены", "Уход"]  # ответ — из своего модуля
    assert indexer.searches == 1
    assert shadow_count("partial") == before[0] + 1
    assert shadow_count("same") == before[1]


def test_overlap() -> None:
    page = found("Цены", "https://a.example/prices")
    local = [{"title": "Цены", "text": "", "url": "https://a.example/prices"}]

    assert overlap(local, [page]) == 1.0
    assert overlap(local, []) == 0.0
    assert overlap([], []) == 1.0  # оба ничего не нашли — совпали


async def test_version_combines_own_and_site(redis: FakeAsyncRedis) -> None:
    indexer = FakeIndexer(version="9:x")

    async def local() -> str:
        return "2:y"

    version = KnowledgeVersion(local, redis, indexer, SALON)

    assert await version() == "2:y|site:9:x"
    assert await version() == "2:y|site:9:x"
    assert indexer.versions == 1  # дальше — из Redis
    await redis.set(site_version_key(SALON), "10:z")  # пришло knowledge.updated
    assert await version() == "2:y|site:10:z"


async def test_unknown_version_disables_cache(redis: FakeAsyncRedis) -> None:
    async def local() -> str:
        return "2:y"

    version = KnowledgeVersion(local, redis, FakeIndexer(down=True), SALON)

    assert await version() is None
