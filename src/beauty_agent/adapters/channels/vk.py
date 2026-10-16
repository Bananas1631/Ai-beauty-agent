"""Адаптер ВКонтакте (Callback API): подтверждение сервера, приём сообщений, отправка ответов.

- Сервер подтверждается строкой из настроек сообщества (событие confirmation).
- Секретный ключ Callback API = выведенный секрет канала (Security.webhook_secret, 48 символов):
  в БД его хранить не нужно, а подделать запрос без ключа нельзя.
- Принимаются только личные сообщения пользователей: беседы (peer_id ≥ 2e9) и сообщения сообществ — нет.
- messages.send с random_id из ключа outbox: VK сам отбросит повтор с тем же random_id.
  Токен сообщества уходит в теле POST, а не в адресе — он не попадёт в логи прокси.
"""

import hashlib
from typing import Any

import httpx

from beauty_agent.adapters.channels.base import (
    ChannelRejectedError,
    ChannelUnavailableError,
    ChannelUpdate,
    Delivery,
    UpdateKind,
    clip,
)
from beauty_agent.domain.enums import Channel

API_URL = "https://api.vk.com/method"
API_VERSION = "5.199"
SECRET_LENGTH = 48
_CHAT_PEER_START = 2_000_000_000
_RETRYABLE = {1, 6, 9, 10}  # неизвестная ошибка, слишком часто, flood control, внутренняя ошибка
MAX_MESSAGE_CHARS = 4096


def vk_secret(full_secret: str) -> str:
    return full_secret[:SECRET_LENGTH]


def parse_event(payload: dict[str, Any]) -> ChannelUpdate | None:
    """Событие message_new → ChannelUpdate. Остальные события агенту не нужны."""
    if payload.get("type") != "message_new":
        return None
    message = (payload.get("object") or {}).get("message")
    if not isinstance(message, dict):
        return None
    peer_id = int(message.get("peer_id") or 0)
    from_id = int(message.get("from_id") or 0)
    if from_id <= 0 or peer_id >= _CHAT_PEER_START or message.get("out"):
        return None
    text = message.get("text")
    photo = _photo_url(message.get("attachments"))
    if photo is not None:
        kind = UpdateKind.PHOTO
    else:
        kind = UpdateKind.TEXT if isinstance(text, str) and text.strip() else UpdateKind.UNSUPPORTED
    if kind is UpdateKind.TEXT and text and text.strip().lower() in {"начать", "start"}:
        kind = UpdateKind.START
    return ChannelUpdate(
        channel=Channel.VK,
        update_id=str(payload.get("event_id") or f"{peer_id}:{message.get('id')}"),
        chat_id=str(peer_id),
        user_id=str(from_id),
        message_id=str(message.get("id") or message.get("conversation_message_id")),
        kind=kind,
        text=clip(text) if kind is not UpdateKind.UNSUPPORTED and isinstance(text, str) and text.strip() else None,
        photo=photo,
    )


def _photo_url(attachments: object) -> str | None:
    """Ссылка на самый крупный размер первого фото во вложениях; только https."""
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if not isinstance(attachment, dict) or attachment.get("type") != "photo":
            continue
        sizes = (attachment.get("photo") or {}).get("sizes") or []
        candidates = [
            size for size in sizes if isinstance(size, dict) and str(size.get("url", "")).startswith("https://")
        ]
        if candidates:
            best = max(candidates, key=lambda size: int(size.get("width") or 0) * int(size.get("height") or 0))
            return str(best["url"])
    return None


def random_id(dedupe_key: str) -> int:
    """Стабильный random_id для messages.send: один и тот же ответ — одно сообщение в VK."""
    return int.from_bytes(hashlib.sha256(dedupe_key.encode()).digest()[:4]) & 0x7FFFFFFF


class VkApi:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str = API_URL) -> None:
        self.http = http
        self.base_url = base_url

    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        token = str(credentials.get("token", ""))
        if not token:
            raise ChannelRejectedError("Нет токена сообщества VK")
        data = {
            "access_token": token,
            "v": API_VERSION,
            "peer_id": delivery.chat_id,
            "message": delivery.text[:MAX_MESSAGE_CHARS],
            "random_id": str(random_id(delivery.dedupe_key or delivery.text)),
        }
        try:
            response = await self.http.post(f"{self.base_url}/messages.send", data=data, timeout=15.0)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChannelUnavailableError(f"VK недоступен: {type(exc).__name__}") from exc
        error = body.get("error")
        if error:
            code = int(error.get("error_code") or 0)
            if code in _RETRYABLE:
                raise ChannelUnavailableError(f"VK: {code}")
            raise ChannelRejectedError(f"VK отклонил сообщение: {code}")
        result = body.get("response")
        return str(result) if result is not None else None
