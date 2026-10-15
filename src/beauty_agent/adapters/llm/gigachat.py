"""Резервный клиент GigaChat на httpx: выручает, когда основная модель недоступна.

- OAuth-токен живёт 30 минут: кэшируется и обновляется за минуту до истечения и сразу после 401.
- Инструменты передаются в формате functions; за один ответ GigaChat вызывает не больше одной.
- Результат инструмента возвращается сообщением role="function" с именем функции.
- Сертификат API выпущен НУЦ Минцифры: путь к корневому сертификату — LLM_GIGACHAT_CA_BUNDLE.
  Проверку TLS не отключаем никогда: иначе ключ авторизации и диалоги уйдут любому посреднику.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from beauty_agent.adapters.llm.base import FREE, ChatMessage, LLMResponse, ModelPrice, ToolCallRequest, ToolSpec, Usage
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

OAUTH_URL = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
API_URL = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"
TOKEN_MARGIN_S = 60


def _arguments(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def to_gigachat_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            call = message.tool_calls[0]  # GigaChat ведёт по одному вызову за ответ
            converted.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "function_call": {"name": call.name, "arguments": _arguments(call.arguments)},
                    "functions_state_id": call.id,
                }
            )
        elif message.role == "tool":
            converted.append({"role": "function", "name": message.name or "tool", "content": message.content or "{}"})
        else:
            converted.append({"role": message.role, "content": message.content or ""})
    return converted


def to_gigachat_functions(tools: Sequence[ToolSpec]) -> list[dict[str, Any]]:
    return [{"name": tool.name, "description": tool.description, "parameters": tool.parameters} for tool in tools]


class GigaChatClient:
    def __init__(
        self,
        *,
        model_key: str,
        model: str,
        auth_key: str,
        scope: str = "GIGACHAT_API_PERS",
        price: ModelPrice = FREE,
        timeout_s: float = 30.0,
        ca_bundle: str | None = None,
        http: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._model_key = model_key
        self.model = model
        self.price = price
        self._auth_key = auth_key
        self._scope = scope
        self._clock = clock
        self._http = http or httpx.AsyncClient(timeout=timeout_s, verify=ca_bundle or True, follow_redirects=False)
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def model_key(self) -> str:
        return self._model_key

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _access_token(self, *, force: bool = False) -> str:
        # Под замком: десять одновременных запросов получат один новый токен, а не десять
        async with self._lock:
            if not force and self._token and self._clock() < self._expires_at - TOKEN_MARGIN_S:
                return self._token
            try:
                response = await self._http.post(
                    OAUTH_URL,
                    headers={
                        "Authorization": f"Basic {self._auth_key}",
                        "RqUID": str(uuid.uuid4()),
                        "Accept": "application/json",
                    },
                    data={"scope": self._scope},
                )
            except httpx.HTTPError as exc:
                raise LLMUnavailableError(f"{self.model_key}: OAuth недоступен") from exc
            if response.status_code != 200:
                raise LLMUnavailableError(f"{self.model_key}: OAuth вернул {response.status_code}")
            try:
                body = response.json()
                self._token = str(body["access_token"])
                self._expires_at = float(body["expires_at"]) / 1000  # приходит в миллисекундах
            except (ValueError, KeyError, TypeError) as exc:
                raise LLMBadResponseError(f"{self.model_key}: неожиданный ответ OAuth") from exc
            return self._token

    async def _post(self, payload: dict[str, Any], token: str) -> httpx.Response:
        try:
            return await self._http.post(API_URL, headers={"Authorization": f"Bearer {token}"}, json=payload)
        except httpx.HTTPError as exc:
            raise LLMUnavailableError(f"{self.model_key}: {type(exc).__name__}") from exc

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec] = (),
        *,
        temperature: float = 0.2,
        max_tokens: int = 800,
    ) -> LLMResponse:
        started = time.perf_counter()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": to_gigachat_messages(messages),
            "temperature": max(temperature, 0.01),  # ровно 0 GigaChat не принимает
            "max_tokens": max_tokens,
        }
        if tools:
            payload["functions"] = to_gigachat_functions(tools)
            payload["function_call"] = "auto"

        response = await self._post(payload, await self._access_token())
        if response.status_code == 401:  # токен отозван раньше срока
            response = await self._post(payload, await self._access_token(force=True))
        if response.status_code in (401, 403, 429) or response.status_code >= 500:
            raise LLMUnavailableError(f"{self.model_key}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise LLMBadResponseError(f"{self.model_key}: HTTP {response.status_code}")
        try:
            return self._parse(response.json(), started)
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMBadResponseError(f"{self.model_key}: неожиданный формат ответа") from exc

    def _parse(self, body: dict[str, Any], started: float) -> LLMResponse:
        message = body["choices"][0]["message"]
        calls: tuple[ToolCallRequest, ...] = ()
        function_call = message.get("function_call")
        if function_call:
            arguments = function_call.get("arguments") or {}
            calls = (
                ToolCallRequest(
                    id=str(message.get("functions_state_id") or uuid.uuid4()),
                    name=str(function_call["name"]),
                    arguments=arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False),
                ),
            )
        raw_usage = body.get("usage") or {}
        usage = Usage(int(raw_usage.get("prompt_tokens", 0)), int(raw_usage.get("completion_tokens", 0)))
        return LLMResponse(
            text=(message.get("content") or "").strip() or None,
            tool_calls=calls,
            usage=usage,
            cost_rub=self.price.cost(usage),
            latency_ms=int((time.perf_counter() - started) * 1000),
            model=self.model_key,
        )
