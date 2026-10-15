"""Клиент OpenAI-совместимых API: Qwen3-235B в Cloud.ru, YandexGPT, свой vLLM.

Повторы SDK выключены (max_retries=0): повторами и переходом на резервную модель занимается
роутер, иначе задержка умножилась бы. Из ответа вырезаются блоки размышлений <think>…</think>.
"""

import re
import time
from collections.abc import Sequence
from typing import Any

import openai
from openai import AsyncOpenAI

from beauty_agent.adapters.llm.base import FREE, ChatMessage, LLMResponse, ModelPrice, ToolCallRequest, ToolSpec, Usage
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

# незакрытый <think> в конце — модель упёрлась в max_tokens посреди размышлений
_THINK = re.compile(r"<think>.*?(?:</think>|\Z)", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str | None) -> str | None:
    """Убирает <think>…</think>; пустой остаток — None."""
    if text is None:
        return None
    cleaned = _THINK.sub("", text).strip()
    return cleaned or None


def to_openai_message(message: ChatMessage) -> dict[str, Any]:
    data: dict[str, Any] = {"role": message.role, "content": message.content or ""}
    if message.role == "assistant" and message.tool_calls:
        data["tool_calls"] = [
            {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": call.arguments}}
            for call in message.tool_calls
        ]
    if message.role == "tool":
        data["tool_call_id"] = message.tool_call_id
    return data


def to_openai_tool(tool: ToolSpec) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
    }


class OpenAICompatClient:
    def __init__(
        self,
        *,
        model_key: str,
        model: str,
        base_url: str,
        api_key: str,
        price: ModelPrice = FREE,
        timeout_s: float = 30.0,
        project: str | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        self._model_key = model_key
        self.model = model
        self.price = price
        self._client = client or AsyncOpenAI(
            api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0, project=project
        )

    @property
    def model_key(self) -> str:
        return self._model_key

    async def aclose(self) -> None:
        await self._client.close()

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [to_openai_message(message) for message in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            kwargs["tools"] = [to_openai_tool(tool) for tool in tools]
        try:
            response = await self._client.chat.completions.create(**kwargs)
        except (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError) as exc:
            raise LLMUnavailableError(f"{self.model_key}: {type(exc).__name__}") from exc
        except openai.APIStatusError as exc:
            # 401/403/404 — ключ или модель недоступны: резервная модель поможет, повтор этой — нет
            if exc.status_code >= 500 or exc.status_code in (401, 403, 404):
                raise LLMUnavailableError(f"{self.model_key}: HTTP {exc.status_code}") from exc
            raise LLMBadResponseError(f"{self.model_key}: HTTP {exc.status_code}") from exc
        except openai.APIError as exc:  # ответ не разобрался по схеме SDK
            raise LLMBadResponseError(f"{self.model_key}: {type(exc).__name__}") from exc

        if not response.choices:
            raise LLMBadResponseError(f"{self.model_key}: пустой ответ")
        message = response.choices[0].message
        calls = tuple(
            ToolCallRequest(id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
            for call in message.tool_calls or []
            if call.type == "function"
        )
        usage = Usage(
            prompt_tokens=response.usage.prompt_tokens if response.usage else 0,
            completion_tokens=response.usage.completion_tokens if response.usage else 0,
        )
        return LLMResponse(
            text=strip_thinking(message.content),
            tool_calls=calls,
            usage=usage,
            cost_rub=self.price.cost(usage),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=self.model_key,
        )
