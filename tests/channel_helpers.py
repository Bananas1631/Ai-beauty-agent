"""Заглушки для тестов мессенджеров: очередь, отправитель, уведомления и Bot API Telegram."""

import json
import uuid
from typing import Any

import httpx

from beauty_agent.adapters.channels.base import Delivery
from beauty_agent.adapters.channels.telegram import TelegramBotApi
from beauty_agent.conversation.notifications import AdminEvent
from beauty_agent.core.config import SecuritySettings
from beauty_agent.core.security import Security

TOKEN = "123456789:AAH-valid_token_for_tests_000000000000"
ADMIN_TOKEN = "987654321:AAH-admin_bot_token_for_tests_0000000"
SECURITY = Security(SecuritySettings(), env="test")


class RecordingQueue:
    def __init__(self) -> None:
        self.scheduled: list[tuple[uuid.UUID, str, str]] = []
        self.admin_updates: list[dict[str, Any]] = []
        self.synced: list[tuple[uuid.UUID, str]] = []
        self.chats: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.reindexed: list[tuple[uuid.UUID, uuid.UUID]] = []
        self.photos: list[tuple[uuid.UUID, dict[str, Any], str]] = []

    async def schedule(self, account_id: uuid.UUID, chat_id: str, token: str, *, delay_s: float) -> None:
        self.scheduled.append((account_id, chat_id, token))

    async def submit_admin_update(self, payload: dict[str, Any]) -> None:
        self.admin_updates.append(payload)

    async def sync_booking(self, salon_id: uuid.UUID, external_id: str) -> None:
        self.synced.append((salon_id, external_id))

    async def process_chat(self, salon_id: uuid.UUID, message_id: uuid.UUID) -> None:
        self.chats.append((salon_id, message_id))

    async def reindex_site(self, salon_id: uuid.UUID, job_id: uuid.UUID) -> None:
        self.reindexed.append((salon_id, job_id))

    async def process_photo(self, account_id: uuid.UUID, update: dict[str, Any], text: str) -> None:
        self.photos.append((account_id, update, text))


class RecordingSender:
    def __init__(self, *failures: Exception, token: str | None = TOKEN) -> None:
        self.sent: list[Delivery] = []
        self.failures = list(failures)
        self.token = token

    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        if self.token is not None:
            assert credentials == {"token": self.token}  # учётные данные расшифрованы из БД
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(delivery)
        return f"msg-{len(self.sent)}"


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[AdminEvent] = []

    async def notify(self, event: AdminEvent) -> None:
        self.events.append(event)


class FakeBotApi:
    """Bot API Telegram на httpx.MockTransport: запоминает вызовы, отвечает ok с растущим message_id."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []  # метод, тело, message_id ответа
        self.results = results or {}  # свой ответ для метода: {"getMe": {"id": 1, "username": "bot"}}
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._handle))
        self.api = TelegramBotApi(self.http)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        message_id = 1000 + len(self.calls)
        self.calls.append((method, json.loads(request.content or b"{}"), message_id))
        result = self.results.get(method, {"message_id": message_id})
        return httpx.Response(200, json={"ok": True, "result": result})

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [payload for name, payload, _ in self.calls if name == method]

    def last_message_id(self) -> int:
        return [message_id for name, _, message_id in self.calls if name == "sendMessage"][-1]


def telegram_update(update_id: int, text: str | None, *, chat_id: int = 555) -> dict[str, Any]:
    message: dict[str, Any] = {
        "message_id": update_id,
        "from": {"id": chat_id, "is_bot": False, "first_name": "Анна"},
        "chat": {"id": chat_id, "type": "private"},
    }
    if text is None:
        message["voice"] = {"file_id": "v"}
    else:
        message["text"] = text
    return {"update_id": update_id, "message": message}
