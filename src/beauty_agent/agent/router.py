"""Роутер LLM: основная модель, при её недоступности — следующая по списку.

Порядок моделей берётся из настроек салона (SalonSettings.llm_models), иначе — LLM_DEFAULT_MODELS:
новую модель или версию промпта можно сначала включить одному салону.
Ту же модель роутер повторно не вызывает: при таймауте в 30 секунд повтор удвоил бы задержку —
быстрее и надёжнее сразу перейти к резервной.
"""

from collections.abc import Mapping, Sequence

import structlog

from beauty_agent.adapters.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from beauty_agent.core import metrics
from beauty_agent.domain.errors import LLMUnavailableError

logger = structlog.get_logger(__name__)


class LLMRouter:
    def __init__(self, clients: Sequence[LLMClient]) -> None:
        if not clients:
            raise ValueError("Нужна хотя бы одна модель")
        self.clients = list(clients)

    @property
    def model_key(self) -> str:
        return self.clients[0].model_key

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        last_error: LLMUnavailableError | None = None
        for index, client in enumerate(self.clients):
            try:
                return await client.complete(messages, tools, temperature=temperature, max_tokens=max_tokens)
            except LLMUnavailableError as exc:
                metrics.provider_error(f"llm:{client.model_key}", "unavailable")
                last_error = exc
                logger.warning("llm_fallback", failed=client.model_key, next_index=index + 1, error=str(exc))
        raise LLMUnavailableError("Все модели недоступны") from last_error


def build_router(
    clients: Mapping[str, LLMClient], order: Sequence[str], *, fallback: LLMClient | None = None
) -> LLMRouter:
    """Роутер по списку имён; ненастроенные модели пропускаются. Ни одной — fallback (офлайн-модель)."""
    chosen = [clients[key] for key in order if key in clients]
    if not chosen and fallback is not None:
        chosen = [fallback]
    return LLMRouter(chosen)
