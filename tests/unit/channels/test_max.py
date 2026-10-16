"""Адаптер MAX: разбор сохранённых обновлений и отправка на моке Bot API."""

import json
from pathlib import Path
from typing import Any

import certifi
import httpx
import pytest
import respx

from beauty_agent.adapters.channels.base import ChannelRejectedError, ChannelUnavailableError, Delivery, UpdateKind
from beauty_agent.adapters.channels.max import API_URL, MaxBotApi, parse_update, tls_context
from beauty_agent.domain.enums import Channel

FIXTURES = Path(__file__).parents[2] / "fixtures" / "max"
TOKEN = "max-bot-token-for-tests"


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def test_text_message() -> None:
    update = parse_update(load("message_created"))

    assert update is not None
    assert (update.channel, update.chat_id, update.user_id) == (Channel.MAX, "9000123", "40021")
    assert update.kind is UpdateKind.TEXT
    assert update.text == "Хочу на маникюр в пятницу"
    assert update.message_id == "mid.0000000000ab12cd"
    assert update.update_id == "msg:mid.0000000000ab12cd"  # повтор вебхука отсечётся по ID сообщения
    assert update.user_name == "Анна"


def test_bot_started_is_first_touch() -> None:
    update = parse_update(load("bot_started"))

    assert update is not None
    assert update.kind is UpdateKind.START
    assert update.chat_id == "9000123"


def test_voice_is_unsupported_and_group_is_ignored() -> None:
    voice = parse_update(load("voice"))

    assert voice is not None
    assert voice.kind is UpdateKind.UNSUPPORTED
    assert voice.text is None
    assert parse_update(load("group_message")) is None  # группы агенту не нужны


@pytest.mark.parametrize(
    "payload",
    [
        {"update_type": "message_edited"},
        {"update_type": "message_created"},
        {"update_type": "message_created", "message": {"recipient": {"chat_type": "dialog"}, "body": {}}},
        {"update_type": "bot_started", "user": {"user_id": 1, "is_bot": True}, "chat_id": 5},
    ],
)
def test_irrelevant_updates(payload: dict[str, Any]) -> None:
    assert parse_update(payload) is None


def test_bot_sender_is_ignored() -> None:
    payload = load("message_created")
    payload["message"]["sender"]["is_bot"] = True

    assert parse_update(payload) is None


@respx.mock
async def test_send_puts_token_in_header() -> None:
    route = respx.post(f"{API_URL}/messages").mock(
        return_value=httpx.Response(200, json={"message": {"body": {"mid": "mid.reply-1"}}})
    )
    async with httpx.AsyncClient() as http:
        result = await MaxBotApi(http).send({"token": TOKEN}, Delivery("9000123", "Здравствуйте!"))

    assert result == "mid.reply-1"
    request = route.calls.last.request
    assert request.headers["Authorization"] == TOKEN
    assert TOKEN not in str(request.url)  # токен не попадёт в логи прокси
    assert request.url.params["chat_id"] == "9000123"
    assert json.loads(request.content)["text"] == "Здравствуйте!"


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(429, json={}), ChannelUnavailableError),
        (httpx.Response(503, json={}), ChannelUnavailableError),
        (httpx.Response(403, json={"code": "chat.denied"}), ChannelRejectedError),
        (httpx.ConnectError("boom"), ChannelUnavailableError),
    ],
)
async def test_send_errors(response: httpx.Response | Exception, error: type[Exception]) -> None:
    respx.post(f"{API_URL}/messages").mock(side_effect=[response])
    async with httpx.AsyncClient() as http:
        with pytest.raises(error):
            await MaxBotApi(http).send({"token": TOKEN}, Delivery("1", "текст"))


async def test_send_without_token_is_rejected() -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ChannelRejectedError):
            await MaxBotApi(http).send({}, Delivery("1", "текст"))


@respx.mock
async def test_subscribe_validates_url_and_secret() -> None:
    route = respx.post(f"{API_URL}/subscriptions").mock(return_value=httpx.Response(200, json={"success": True}))
    async with httpx.AsyncClient() as http:
        api = MaxBotApi(http)
        with pytest.raises(ChannelRejectedError, match="HTTPS"):
            await api.subscribe(TOKEN, "http://example.ru/hook", "a" * 64)
        with pytest.raises(ChannelRejectedError, match="Секрет"):
            await api.subscribe(TOKEN, "https://example.ru/hook", "bad secret!")
        await api.subscribe(TOKEN, "https://example.ru/hook", "a" * 64)

    body = json.loads(route.calls.last.request.content)
    assert body == {
        "url": "https://example.ru/hook",
        "update_types": ["message_created", "bot_started"],
        "secret": "a" * 64,
    }


def test_tls_context_adds_extra_root() -> None:
    plain = tls_context(None)
    extended = tls_context(certifi.where())  # в проде — корневой сертификат Минцифры

    assert plain.verify_mode == extended.verify_mode
    assert extended.check_hostname  # проверка TLS не отключается
