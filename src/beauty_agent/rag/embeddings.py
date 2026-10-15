"""Эмбеддинги — векторы текста: по ним ищутся фрагменты, близкие по смыслу к вопросу клиента.

- OpenAICompatEmbeddings — облачная модель (bge-m3 в Cloud.ru): пачками, с учётом стоимости.
- HashingEmbeddings — офлайн без ключей: слова хешируются в вектор. Синонимов не понимает,
  но вместе с полнотекстовым поиском даёт рабочий RAG для демо, тестов и CI.

Размерность фиксирована (1024) и совпадает с колонкой knowledge_chunks.embedding.
"""

import hashlib
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import openai
from openai import AsyncOpenAI

from beauty_agent.adapters.llm.base import FREE, ModelPrice, Usage
from beauty_agent.agent.tokens import estimate_tokens
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

DIMENSIONS = 1024
BATCH_SIZE = 32


@dataclass(frozen=True, slots=True)
class Embedded:
    vectors: list[list[float]]
    tokens: int
    cost_rub: Decimal


class EmbeddingsClient(Protocol):
    @property
    def model_key(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> Embedded: ...


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else vector


class HashingEmbeddings:
    """Мешок слов с хешированием: основа слова → индекс в векторе. Детерминирован и бесплатен."""

    model_key = "hashing"
    _WORD = re.compile(r"[a-zа-я0-9]+")

    async def embed(self, texts: Sequence[str]) -> Embedded:
        vectors = [self.vector(text) for text in texts]
        return Embedded(vectors, sum(estimate_tokens(text) for text in texts), Decimal(0))

    def vector(self, text: str) -> list[float]:
        vector = [0.0] * DIMENSIONS
        for word in self._WORD.findall(text.lower().replace("ё", "е")):
            stem = word[:5] if len(word) > 5 else word  # грубая основа: «стрижка» и «стрижку» — одно
            digest = hashlib.blake2b(stem.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4]) % DIMENSIONS
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[index] += sign
        return _normalize(vector)


class OpenAICompatEmbeddings:
    def __init__(
        self,
        *,
        model_key: str,
        model: str,
        base_url: str,
        api_key: str,
        price: ModelPrice = FREE,
        timeout_s: float = 30.0,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model_key = model_key
        self.model = model
        self.price = price
        self._client = client or AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=2)

    @property
    def model_key(self) -> str:
        return self._model_key

    async def embed(self, texts: Sequence[str]) -> Embedded:
        vectors: list[list[float]] = []
        tokens = 0
        for start in range(0, len(texts), BATCH_SIZE):
            batch = list(texts[start : start + BATCH_SIZE])
            try:
                response = await self._client.embeddings.create(model=self.model, input=batch)
            except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError) as exc:
                raise LLMUnavailableError(f"{self.model_key}: {type(exc).__name__}") from exc
            except openai.APIError as exc:
                raise LLMBadResponseError(f"{self.model_key}: {type(exc).__name__}") from exc
            ordered = sorted(response.data, key=lambda item: item.index)
            for item in ordered:
                if len(item.embedding) != DIMENSIONS:
                    raise LLMBadResponseError(
                        f"{self.model_key}: размерность {len(item.embedding)}, ждали {DIMENSIONS}"
                    )
                vectors.append(_normalize(list(item.embedding)))
            tokens += response.usage.prompt_tokens if response.usage else sum(estimate_tokens(t) for t in batch)
        return Embedded(vectors, tokens, self.price.cost(Usage(prompt_tokens=tokens)))
