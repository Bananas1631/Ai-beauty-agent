import json
from typing import Any

import httpx
import pytest

from beauty_agent.adapters.channels.base import ChannelRejectedError, ChannelUnavailableError, Delivery, UpdateKind
from beauty_agent.adapters.channels.telegram import TelegramBotApi, check_token, parse_update
from beauty_agent.adapters.channels.telegram_business import parse_business_connection
from beauty_agent.adapters.channels.vk import VkApi, parse_event, random_id, vk_secret
from beauty_agent.domain.enums import Channel

TOKEN = "123456789:AAH-valid_token_for_tests_000000000000"


def tg_message(text: str | None = "Хочу на маникюр", *, chat_type: str = "private", **extra: Any) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": 10,
        "from": {"id": 555, "is_bot": False, "first_name": "Анна"},
        "chat": {"id": 555, "type": chat_type},
        "date": 1,
        **extra,
    }
    if text is not None:
        message["text"] = text
    return {"update_id": 9001, "message": message}


# ---------------------------------------------------------------- Telegram


def test_text_message() -> None:
    update = parse_update(tg_message())

    assert update is not None
    assert (update.channel, update.chat_id, update.user_id, update.message_id) == (Channel.TELEGRAM, "555", "555", "10")
    assert update.kind is UpdateKind.TEXT
    assert update.text == "Хочу на маникюр"
    assert update.user_name == "Анна"
    assert update.update_id == "9001"


def test_start_voice_group_and_bot() -> None:
    assert parse_update(tg_message("/start"))
    assert parse_update(tg_message("/start")).kind is UpdateKind.START  # type: ignore[union-attr]
    voice = parse_update(tg_message(None, voice={"file_id": "x", "duration": 3}))
    assert voice is not None
    assert voice.kind is UpdateKind.UNSUPPORTED
    assert voice.text is None
    assert parse_update(tg_message(chat_type="group")) is None  # группы агенту не нужны
    bot = tg_message()
    bot["message"]["from"]["is_bot"] = True
    assert parse_update(bot) is None
    assert parse_update({"update_id": 1, "edited_message": {}}) is None


def business(sender_id: int, **extra: Any) -> dict[str, Any]:
    return {
        "update_id": 7,
        "business_message": {
            "message_id": 3,
            "business_connection_id": "bc-1",
            "from": {"id": sender_id, "is_bot": False, "first_name": "Вера"},
            "chat": {"id": 777, "type": "private"},
            "text": "Есть окна в субботу?",
            **extra,
        },
    }


def test_business_messages() -> None:
    client = parse_update(business(777))
    assert client is not None
    assert client.channel is Channel.TELEGRAM_BUSINESS
    assert client.route == {"business_connection_id": "bc-1"}
    assert parse_update(business(100)) is None  # написал владелец аккаунта салона, а не клиент
    assert parse_update(business(777, sender_business_bot={"id": 1})) is None  # ответ нашего же бота


def test_business_connection_event() -> None:
    event = {"business_connection": {"id": "bc-1", "user": {"id": 100}, "is_enabled": True, "can_reply": True}}

    assert parse_business_connection(event) == {
        "connection_id": "bc-1",
        "owner_id": "100",
        "enabled": True,
        "can_reply": True,
    }


@pytest.mark.parametrize("token", ["", "abc", "123:short", "123456789:AAH/../../etc/passwd________________"])
def test_bad_tokens_never_reach_url(token: str) -> None:
    with pytest.raises(ChannelRejectedError):
        check_token(token)


def telegram_api(handler: Any) -> TelegramBotApi:
    return TelegramBotApi(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_send_with_business_connection() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    message_id = await telegram_api(handle).send(
        {"token": TOKEN}, Delivery("777", "Ответ", {"business_connection_id": "bc-1"})
    )

    assert message_id == "42"
    assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
    assert json.loads(seen[0].content) == {"chat_id": "777", "text": "Ответ", "business_connection_id": "bc-1"}


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (429, {"ok": False, "error_code": 429, "description": "Too Many Requests"}, ChannelUnavailableError),
        (502, {"ok": False, "error_code": 502}, ChannelUnavailableError),
        (403, {"ok": False, "error_code": 403, "description": "bot was blocked by the user"}, ChannelRejectedError),
    ],
)
async def test_send_errors(status: int, body: dict[str, Any], error: type[Exception]) -> None:
    with pytest.raises(error):
        await telegram_api(lambda _r: httpx.Response(status, json=body)).send({"token": TOKEN}, Delivery("1", "x"))


async def test_network_error_is_retryable() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("нет сети")

    with pytest.raises(ChannelUnavailableError):
        await telegram_api(broken).send({"token": TOKEN}, Delivery("1", "x"))


async def test_webhook_and_polling_calls() -> None:
    methods: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        methods.append(request.url.path.rsplit("/", 1)[-1])
        result: Any = [{"update_id": 1}] if methods[-1] == "getUpdates" else True
        return httpx.Response(200, json={"ok": True, "result": result})

    api = telegram_api(handle)
    await api.set_webhook(TOKEN, "https://app.example/api/v1/webhooks/telegram/x", "s" * 64)
    updates = await api.get_updates(TOKEN, offset=5, timeout_s=1)
    await api.delete_webhook(TOKEN)

    assert methods == ["setWebhook", "getUpdates", "deleteWebhook"]
    assert updates == [{"update_id": 1}]


# ---------------------------------------------------------------- VK


def vk_event(text: str = "Здравствуйте", *, peer_id: int = 321, from_id: int = 321, **extra: Any) -> dict[str, Any]:
    return {
        "type": "message_new",
        "event_id": "ev-1",
        "group_id": 1,
        "object": {"message": {"id": 99, "peer_id": peer_id, "from_id": from_id, "text": text, **extra}},
    }


def test_vk_events() -> None:
    update = parse_event(vk_event())
    assert update is not None
    assert (update.channel, update.chat_id, update.update_id, update.kind) == (
        Channel.VK,
        "321",
        "ev-1",
        UpdateKind.TEXT,
    )
    assert parse_event(vk_event("Начать")).kind is UpdateKind.START  # type: ignore[union-attr]
    assert parse_event(vk_event("", attachments=[{"type": "audio_message"}])).kind is UpdateKind.UNSUPPORTED  # type: ignore[union-attr]
    assert parse_event(vk_event(peer_id=2_000_000_001)) is None  # беседа
    assert parse_event(vk_event(from_id=-5)) is None  # сообщение сообщества
    assert parse_event({"type": "group_join"}) is None


def test_vk_random_id_is_stable() -> None:
    assert random_id("vk:a:321:99") == random_id("vk:a:321:99")
    assert random_id("vk:a:321:99") != random_id("vk:a:321:100")
    assert 0 <= random_id("x") < 2**31
    assert len(vk_secret("a" * 64)) == 48


async def test_vk_send_uses_post_body() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"response": 555})

    api = VkApi(httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    message_id = await api.send({"token": "vk-token"}, Delivery("321", "Ответ", dedupe_key="k"))

    assert message_id == "555"
    assert "vk-token" not in str(seen[0].url)  # токен — в теле, а не в адресе
    assert b"access_token=vk-token" in seen[0].content


@pytest.mark.parametrize(
    ("code", "error"), [(6, ChannelUnavailableError), (901, ChannelRejectedError), (5, ChannelRejectedError)]
)
async def test_vk_errors(code: int, error: type[Exception]) -> None:
    api = VkApi(
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json={"error": {"error_code": code}}))
        )
    )

    with pytest.raises(error):
        await api.send({"token": "t"}, Delivery("1", "x"))
    with pytest.raises(ChannelRejectedError):
        await api.send({}, Delivery("1", "x"))
