"""LLM-клиент с трассировкой: каждый вызов модели — наблюдение generation в текущей трассе.

В трассу уходят модель (фактическая — после перехода роутера на резервную), токены, стоимость
в рублях, задержка и ошибка провайдера. Без включённой трассировки обёртка ничего не делает.
"""

from collections.abc import Sequence

from beauty_agent.adapters.llm.base import ChatMessage, LLMClient, LLMResponse, ToolSpec
from beauty_agent.core.tracing import observe


class TracedLLM:
    def __init__(self, inner: LLMClient, *, name: str = "llm") -> None:
        self.inner = inner
        self.name = name

    @property
    def model_key(self) -> str:
        return self.inner.model_key

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        payload = [{"role": message.role, "content": message.content} for message in messages]
        with observe(self.name, kind="generation", input=payload, model=self.inner.model_key) as generation:
            response = await self.inner.complete(messages, tools, temperature=temperature, max_tokens=max_tokens)
            generation.update(
                output={
                    "text": response.text,
                    "tool_calls": [{"name": call.name, "arguments": call.arguments} for call in response.tool_calls],
                },
                model=response.model or self.inner.model_key,
                prompt_tokens=response.usage.prompt_tokens,
                completion_tokens=response.usage.completion_tokens,
                cost_rub=response.cost_rub,
                metadata={"latency_ms": response.latency_ms, "tools_offered": len(tools)},
            )
            return response
