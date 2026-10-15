"""Фейковый LLM с заранее заданными ответами: тесты агента быстрые, бесплатные и всегда одинаковые.

Шаг сценария — готовый LLMResponse, функция от (messages, tools) или исключение (имитация сбоя).
"""

import json
from collections.abc import Callable, Iterable, Sequence
from decimal import Decimal
from itertools import count
from typing import Any

from beauty_agent.adapters.llm.base import ChatMessage, LLMResponse, ToolCallRequest, ToolSpec, Usage
from beauty_agent.domain.errors import LLMBadResponseError

Responder = Callable[[Sequence[ChatMessage], Sequence[ToolSpec]], LLMResponse]
Step = LLMResponse | Responder | Exception

_call_ids = count(1)


def reply(text: str, *, prompt_tokens: int = 400, completion_tokens: int = 40, cost: str = "0.01") -> LLMResponse:
    """Шаг сценария: модель отвечает текстом."""
    return LLMResponse(text, (), Usage(prompt_tokens, completion_tokens), Decimal(cost), latency_ms=5, model="fake")


def call_tool(name: str, arguments: dict[str, Any] | str, *, call_id: str | None = None) -> LLMResponse:
    """Шаг сценария: модель вызывает инструмент. arguments-строка — для проверки кривого JSON."""
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    call = ToolCallRequest(call_id or f"call_{next(_call_ids)}", name, raw)
    return LLMResponse(None, (call,), Usage(400, 20), Decimal("0.01"), latency_ms=5, model="fake")


class ScriptedLLM:
    def __init__(self, script: Iterable[Step] = (), *, model_key: str = "fake") -> None:
        self._model_key = model_key
        self.script: list[Step] = list(script)
        self.calls: list[tuple[list[ChatMessage], list[ToolSpec]]] = []

    @property
    def model_key(self) -> str:
        return self._model_key

    def add(self, *steps: Step) -> None:
        self.script.extend(steps)

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        self.calls.append((list(messages), list(tools)))
        if not self.script:
            raise LLMBadResponseError("ScriptedLLM: сценарий закончился")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages, tools)
        return step
