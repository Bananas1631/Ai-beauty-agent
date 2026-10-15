import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import httpx
import httpx2
import pytest
import respx
from openai import AsyncOpenAI
from pydantic import SecretStr

from beauty_agent.adapters.llm.base import ChatMessage, ModelPrice, ToolCallRequest, ToolSpec, Usage
from beauty_agent.adapters.llm.fake import ScriptedLLM, call_tool, reply
from beauty_agent.adapters.llm.gigachat import API_URL, OAUTH_URL, GigaChatClient, to_gigachat_messages
from beauty_agent.adapters.llm.models import build_clients
from beauty_agent.adapters.llm.openai_compat import OpenAICompatClient, strip_thinking
from beauty_agent.core.config import LLMSettings
from beauty_agent.domain.errors import LLMBadResponseError, LLMUnavailableError

BASE = "https://llm.test/v1"
SSL_CONTEXT = httpx.create_ssl_context()
PRICE = ModelPrice(Decimal("0.05"), Decimal("0.20"))
TOOL = ToolSpec("find_service", "Найти услугу", {"type": "object", "properties": {"query": {"type": "string"}}})
MESSAGES = [ChatMessage("system", "Ты администратор"), ChatMessage("user", "Сколько стоит маникюр?")]


def completion(message: dict[str, Any], *, prompt: int = 1000, completion_tokens: int = 100) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 1,
        "model": "qwen",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", **message}}],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens,
        },
    }


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------- общее


def test_price_per_thousand_tokens() -> None:
    assert PRICE.cost(Usage(1000, 100)) == Decimal("0.0700")  # 1000 × 0.05 / 1000 + 100 × 0.2 / 1000
    assert (Usage(1, 2) + Usage(3, 4)).total == 10


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("<think>считаю цену</think>Маникюр стоит 2 500 ₽", "Маникюр стоит 2 500 ₽"),
        ("<THINK>a\nb</THINK>\n Готово", "Готово"),
        ("Ответ<think>оборвалось на max_tokens", "Ответ"),
        ("<think>только размышления</think>", None),
        (None, None),
    ],
)
def test_strip_thinking(text: str | None, expected: str | None) -> None:
    assert strip_thinking(text) == expected


def test_build_clients_only_for_configured_providers() -> None:
    assert build_clients(LLMSettings()) == {}

    settings = LLMSettings(
        cloudru_api_key=SecretStr("a"),
        yandex_api_key=SecretStr("b"),
        yandex_folder_id="folder",
        gigachat_auth_key=SecretStr("c"),
    )
    clients = build_clients(settings)

    assert set(clients) == {"qwen3-235b", "yandexgpt-lite", "yandexgpt-pro", "gigachat-2-lite", "gigachat-2-max"}


# ---------------------------------------------------------------- OpenAI-совместимый клиент
# OpenAI SDK 3.x ходит в сеть через httpx2 — respx его не перехватывает, поэтому подставляем
# свой транспорт httpx2.MockTransport в клиент SDK.


class FakeServer:
    """Отвечает заданными ответами по очереди (последний повторяется) и запоминает запросы."""

    def __init__(self, *responses: httpx2.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx2.Request] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        item = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    def body(self) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[-1].content)
        return data

    def client(self) -> OpenAICompatClient:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self.handle))
        sdk = AsyncOpenAI(api_key="k", base_url=BASE, max_retries=0, http_client=http)
        return OpenAICompatClient(
            model_key="qwen3-235b", model="Qwen/Qwen3", base_url=BASE, api_key="k", price=PRICE, client=sdk
        )


def ok(message: dict[str, Any]) -> httpx2.Response:
    return httpx2.Response(200, json=completion(message))


async def test_text_answer_with_cost() -> None:
    server = FakeServer(ok({"content": "<think>…</think>Маникюр с покрытием — от 2 500 ₽"}))

    response = await server.client().complete(MESSAGES, [TOOL])

    assert response.text == "Маникюр с покрытием — от 2 500 ₽"
    assert response.cost_rub == Decimal("0.0700")
    assert response.model == "qwen3-235b"
    assert str(server.requests[-1].url) == f"{BASE}/chat/completions"
    assert server.body()["tools"][0]["function"]["name"] == "find_service"
    assert server.body()["messages"][1] == {"role": "user", "content": "Сколько стоит маникюр?"}


async def test_tool_calls_are_parsed() -> None:
    call = {"id": "c1", "type": "function", "function": {"name": "find_service", "arguments": '{"query":"маникюр"}'}}
    server = FakeServer(ok({"content": None, "tool_calls": [call]}))

    response = await server.client().complete(MESSAGES, [TOOL])

    assert response.text is None
    assert response.tool_calls == (ToolCallRequest("c1", "find_service", '{"query":"маникюр"}'),)


async def test_tool_result_messages_are_sent_back() -> None:
    server = FakeServer(ok({"content": "Готово"}))
    history = [
        *MESSAGES,
        ChatMessage("assistant", None, (ToolCallRequest("c1", "find_service", "{}"),)),
        ChatMessage("tool", '{"found": 1}', tool_call_id="c1", name="find_service"),
    ]

    await server.client().complete(history)

    sent = server.body()["messages"]
    assert sent[2]["tool_calls"][0]["id"] == "c1"
    assert sent[3] == {"role": "tool", "content": '{"found": 1}', "tool_call_id": "c1"}


@pytest.mark.parametrize("status", [500, 503, 429, 401, 404])
async def test_unavailable_statuses(status: int) -> None:
    server = FakeServer(httpx2.Response(status, json={"error": {"message": "x"}}))

    with pytest.raises(LLMUnavailableError):
        await server.client().complete(MESSAGES)
    assert len(server.requests) == 1  # повторами занимается роутер, а не SDK


async def test_bad_request_is_bad_response() -> None:
    server = FakeServer(httpx2.Response(400, json={"error": {"message": "bad schema"}}))

    with pytest.raises(LLMBadResponseError):
        await server.client().complete(MESSAGES)


async def test_timeout_is_unavailable() -> None:
    server = FakeServer(httpx2.ConnectTimeout("slow"))

    with pytest.raises(LLMUnavailableError):
        await server.client().complete(MESSAGES)


async def test_empty_choices() -> None:
    payload = completion({"content": "x"})
    payload["choices"] = []
    server = FakeServer(httpx2.Response(200, json=payload))

    with pytest.raises(LLMBadResponseError):
        await server.client().complete(MESSAGES)


# ---------------------------------------------------------------- GigaChat


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def token(value: str, expires_in_s: float, clock: Clock) -> dict[str, Any]:
    return {"access_token": value, "expires_at": int((clock.now + expires_in_s) * 1000)}


def gigachat(clock: Clock) -> GigaChatClient:
    http = httpx.AsyncClient(verify=SSL_CONTEXT)
    return GigaChatClient(
        model_key="gigachat-2-lite", model="GigaChat-2", auth_key="basic", price=PRICE, http=http, clock=clock
    )


def giga_answer(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "choices": [{"message": {"role": "assistant", **message}}],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 100},
    }


async def test_gigachat_token_is_cached_and_refreshed(api: respx.MockRouter) -> None:
    clock = Clock()
    oauth = api.post(OAUTH_URL).mock(
        side_effect=[
            httpx.Response(200, json=token("t1", 1800, clock)),
            httpx.Response(200, json=token("t2", 1800, clock)),
        ]
    )
    chat = api.post(API_URL).respond(200, json=giga_answer({"content": "Здравствуйте!"}))
    client = gigachat(clock)

    await client.complete(MESSAGES)
    await client.complete(MESSAGES)
    assert oauth.call_count == 1  # токен живёт 30 минут — второй запрос его не получает

    clock.now += 1800 - 30  # меньше минуты до истечения — обновляем заранее
    await client.complete(MESSAGES)
    assert oauth.call_count == 2
    assert chat.calls.last.request.headers["Authorization"] == "Bearer t2"
    assert oauth.calls.last.request.headers["Authorization"] == "Basic basic"


async def test_gigachat_refreshes_token_after_401(api: respx.MockRouter) -> None:
    clock = Clock()
    api.post(OAUTH_URL).mock(
        side_effect=[
            httpx.Response(200, json=token("t1", 1800, clock)),
            httpx.Response(200, json=token("t2", 1800, clock)),
        ]
    )
    api.post(API_URL).mock(side_effect=[httpx.Response(401), httpx.Response(200, json=giga_answer({"content": "ok"}))])

    response = await gigachat(clock).complete(MESSAGES)

    assert response.text == "ok"


async def test_gigachat_functions(api: respx.MockRouter) -> None:
    clock = Clock()
    api.post(OAUTH_URL).respond(200, json=token("t1", 1800, clock))
    route = api.post(API_URL).respond(
        200,
        json=giga_answer(
            {
                "content": "",
                "function_call": {"name": "find_service", "arguments": {"query": "маникюр"}},
                "functions_state_id": "fs-1",
            }
        ),
    )

    response = await gigachat(clock).complete(MESSAGES, [TOOL], temperature=0)

    assert response.tool_calls == (ToolCallRequest("fs-1", "find_service", '{"query": "маникюр"}'),)
    assert response.cost_rub == Decimal("0.0700")
    body = json.loads(route.calls.last.request.content)
    assert body["functions"] == [{"name": "find_service", "description": "Найти услугу", "parameters": TOOL.parameters}]
    assert body["temperature"] > 0


def test_gigachat_message_conversion() -> None:
    history = [
        ChatMessage("assistant", None, (ToolCallRequest("fs-1", "find_service", '{"query": "маникюр"}'),)),
        ChatMessage("tool", '{"found": 1}', tool_call_id="fs-1", name="find_service"),
    ]

    converted = to_gigachat_messages(history)

    assert converted[0]["function_call"] == {"name": "find_service", "arguments": {"query": "маникюр"}}
    assert converted[0]["functions_state_id"] == "fs-1"
    assert converted[1] == {"role": "function", "name": "find_service", "content": '{"found": 1}'}


@pytest.mark.parametrize("status", [429, 500, 403])
async def test_gigachat_unavailable(api: respx.MockRouter, status: int) -> None:
    clock = Clock()
    api.post(OAUTH_URL).respond(200, json=token("t1", 1800, clock))
    api.post(API_URL).respond(status)

    with pytest.raises(LLMUnavailableError):
        await gigachat(clock).complete(MESSAGES)


async def test_gigachat_oauth_down(api: respx.MockRouter) -> None:
    api.post(OAUTH_URL).respond(503)

    with pytest.raises(LLMUnavailableError):
        await gigachat(Clock()).complete(MESSAGES)


async def test_gigachat_unexpected_payload(api: respx.MockRouter) -> None:
    clock = Clock()
    api.post(OAUTH_URL).respond(200, json=token("t1", 1800, clock))
    api.post(API_URL).respond(200, json={"choices": []})

    with pytest.raises(LLMBadResponseError):
        await gigachat(clock).complete(MESSAGES)


# ---------------------------------------------------------------- фейк


async def test_scripted_llm() -> None:
    llm = ScriptedLLM([call_tool("find_service", {"query": "маникюр"}), reply("Готово"), RuntimeError("сбой")])

    first = await llm.complete(MESSAGES, [TOOL])
    second = await llm.complete(MESSAGES)

    assert first.tool_calls[0].name == "find_service"
    assert json.loads(first.tool_calls[0].arguments) == {"query": "маникюр"}
    assert second.text == "Готово"
    with pytest.raises(RuntimeError):
        await llm.complete(MESSAGES)
    with pytest.raises(LLMBadResponseError):
        await llm.complete(MESSAGES)
    assert len(llm.calls) == 4
