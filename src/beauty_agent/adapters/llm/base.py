"""Единый интерфейс LLM-клиента: агент не знает, какая модель под капотом.

На входе — сообщения и инструменты; на выходе — текст или вызовы инструментов, токены,
стоимость в рублях и задержка. Ошибки провайдеров переводятся в доменные:
LLMUnavailableError (таймаут, 5xx, лимит — роутер переходит к следующей модели)
и LLMBadResponseError (ответ нельзя разобрать).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True, slots=True)
class ToolCallRequest:
    id: str
    name: str
    arguments: str  # JSON-строка как её вернула модель: разбирает и проверяет реестр инструментов


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Role
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    tool_call_id: str | None = None  # для role="tool": на какой вызов это ответ
    name: str | None = None  # для role="tool": имя инструмента (GigaChat требует его)


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema аргументов


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.prompt_tokens + other.prompt_tokens, self.completion_tokens + other.completion_tokens)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """Цена модели в рублях за 1000 токенов: вход и выход считаются отдельно."""

    input_per_1k: Decimal
    output_per_1k: Decimal

    def cost(self, usage: Usage) -> Decimal:
        raw = (
            Decimal(usage.prompt_tokens) * self.input_per_1k + Decimal(usage.completion_tokens) * self.output_per_1k
        ) / 1000
        return raw.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


FREE = ModelPrice(Decimal(0), Decimal(0))


@dataclass(frozen=True, slots=True)
class LLMResponse:
    text: str | None
    tool_calls: tuple[ToolCallRequest, ...] = ()
    usage: Usage = field(default_factory=Usage)
    cost_rub: Decimal = Decimal(0)
    latency_ms: int = 0
    model: str = ""


class LLMClient(Protocol):
    @property
    def model_key(self) -> str:
        """Короткое имя модели в наших настройках: qwen3-235b, gigachat-2-lite…"""
        ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse: ...
