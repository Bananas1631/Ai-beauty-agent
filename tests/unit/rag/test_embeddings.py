import math
from decimal import Decimal
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI

from beauty_agent.adapters.llm.base import ModelPrice
from beauty_agent.adapters.llm.models import build_embedder
from beauty_agent.core.config import LLMSettings
from beauty_agent.domain.errors import LLMBadResponseError
from beauty_agent.rag.embeddings import DIMENSIONS, HashingEmbeddings, OpenAICompatEmbeddings


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


async def test_hashing_is_deterministic_and_normalized() -> None:
    embedder = HashingEmbeddings()

    first = await embedder.embed(["Маникюр с покрытием", "Маникюр с покрытием"])

    assert first.vectors[0] == first.vectors[1]
    assert len(first.vectors[0]) == DIMENSIONS
    assert math.isclose(math.sqrt(sum(v * v for v in first.vectors[0])), 1.0)
    assert first.cost_rub == 0


async def test_hashing_similar_texts_are_closer() -> None:
    embedder = HashingEmbeddings()
    [question, price, rules] = (
        await embedder.embed(["стрижка бороды цена", "Моделирование бороды — 1 200 ₽", "Отмена записи за 24 часа"])
    ).vectors

    assert cosine(question, price) > cosine(question, rules)


def test_embedder_choice() -> None:
    assert isinstance(build_embedder(LLMSettings()), HashingEmbeddings)


def server(*vectors: list[float]) -> AsyncOpenAI:
    def handle(request: httpx2.Request) -> httpx2.Response:
        payload: dict[str, Any] = {
            "object": "list",
            "model": "bge-m3",
            "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }
        return httpx2.Response(200, json=payload)

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
    return AsyncOpenAI(api_key="k", base_url="https://llm.test/v1", max_retries=0, http_client=http)


async def test_cloud_embeddings_with_cost() -> None:
    client = server([3.0, 4.0] + [0.0] * (DIMENSIONS - 2))
    embedder = OpenAICompatEmbeddings(
        model_key="bge-m3",
        model="BAAI/bge-m3",
        base_url="",
        api_key="",
        price=ModelPrice(Decimal(1), Decimal(0)),
        client=client,
    )

    embedded = await embedder.embed(["вопрос"])

    assert embedded.vectors[0][:2] == [0.6, 0.8]  # нормирован
    assert embedded.tokens == 10
    assert embedded.cost_rub == Decimal("0.0100")


async def test_wrong_dimension_is_rejected() -> None:
    embedder = OpenAICompatEmbeddings(model_key="x", model="x", base_url="", api_key="", client=server([1.0, 2.0]))

    with pytest.raises(LLMBadResponseError, match="размерность"):
        await embedder.embed(["вопрос"])
