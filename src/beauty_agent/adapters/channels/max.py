"""Адаптер мессенджера MAX (Bot API platform-api2.max.ru) через общий интерфейс канала.

Сервис диалога и агент не меняются: обновление MAX превращается в тот же ChannelUpdate,
ответ уходит через тот же outbox.

- Токен бота — только в заголовке Authorization: передача в адресе запроса больше не поддерживается
  и попала бы в логи прокси.
- Вебхук: POST /subscriptions с полем secret; MAX присылает его в заголовке X-Max-Bot-Api-Secret.
  Только HTTPS на порту 443 с доверенным сертификатом. Не ответили 200 за 30 секунд — до 10 повторов,
  через 8 часов без успеха MAX отписывает бота.
- Сервер API работает на сертификате Минцифры: путь к корневому сертификату — MAX_CA_BUNDLE
  (docs/integrations/max.md). Проверку TLS не отключаем.
- Принимаются только личные диалоги (chat_type = dialog): группы и каналы агенту не нужны.
- Фото (вложение image) — PHOTO со ссылкой на файл и подписью; голосовые и файлы — UNSUPPORTED,
  вежливый ответ.
"""

import re
import ssl
from pathlib import Path
from typing import Any

import certifi
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

API_URL = "https://platform-api2.max.ru"
MAX_MESSAGE_CHARS = 4000
UPDATE_TYPES = ["message_created", "bot_started"]
_SECRET = re.compile(r"^[A-Za-z0-9_-]{5,256}$")
_RETRYABLE = {429, 500, 502, 503, 504}


def tls_context(ca_bundle: str | Path | None) -> ssl.SSLContext:
    """Публичные корневые сертификаты плюс, если задан, корневой сертификат Минцифры."""
    context = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle:
        context.load_verify_locations(cafile=str(ca_bundle))
    return context


def _user_name(user: dict[str, Any]) -> str | None:
    name = user.get("first_name") or user.get("name")
    return str(name)[:200] if name else None


def parse_update(payload: dict[str, Any]) -> ChannelUpdate | None:
    """Обновление MAX → ChannelUpdate. None — обновление агенту не нужно."""
    kind_name = payload.get("update_type")
    if kind_name == "bot_started":
        user = payload.get("user") or {}
        chat_id = payload.get("chat_id")
        user_id = user.get("user_id")
        if chat_id is None or user_id is None or user.get("is_bot"):
            return None
        stamp = str(payload.get("timestamp") or "")
        return ChannelUpdate(
            channel=Channel.MAX,
            update_id=f"start:{chat_id}:{stamp}",
            chat_id=str(chat_id),
            user_id=str(user_id),
            message_id=f"start:{stamp}",
            kind=UpdateKind.START,
            user_name=_user_name(user),
        )
    if kind_name != "message_created":
        return None
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("sender") or {}
    recipient = message.get("recipient") or {}
    body = message.get("body") or {}
    if recipient.get("chat_type") != "dialog" or sender.get("is_bot") or sender.get("user_id") is None:
        return None
    mid = body.get("mid")
    chat_id = recipient.get("chat_id")
    if not mid or chat_id is None:
        return None
    text = body.get("text")
    photo = _image_url(body.get("attachments"))
    if photo is not None:
        kind = UpdateKind.PHOTO
    elif isinstance(text, str) and text.strip():
        kind = UpdateKind.START if text.strip().lower() in {"/start", "начать"} else UpdateKind.TEXT
    else:
        kind = UpdateKind.UNSUPPORTED
    return ChannelUpdate(
        channel=Channel.MAX,
        update_id=f"msg:{mid}",  # у MAX нет ID обновления: повтор вебхука несёт тот же ID сообщения
        chat_id=str(chat_id),
        user_id=str(sender["user_id"]),
        message_id=str(mid),
        kind=kind,
        text=clip(text) if kind is not UpdateKind.UNSUPPORTED and isinstance(text, str) and text.strip() else None,
        user_name=_user_name(sender),
        photo=photo,
    )


def _image_url(attachments: object) -> str | None:
    if not isinstance(attachments, list):
        return None
    for attachment in attachments:
        if isinstance(attachment, dict) and attachment.get("type") == "image":
            url = str((attachment.get("payload") or {}).get("url") or "")
            if url.startswith("https://"):
                return url
    return None


class MaxBotApi:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str = API_URL) -> None:
        self.http = http
        self.base_url = base_url.rstrip("/")

    async def call(
        self,
        token: str,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout_s: float = 15.0,
    ) -> Any:
        if not token:
            raise ChannelRejectedError("Нет токена бота MAX")
        try:
            response = await self.http.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=json,
                headers={"Authorization": token},
                timeout=timeout_s,
            )
        except httpx.HTTPError as exc:
            raise ChannelUnavailableError(f"MAX недоступен: {type(exc).__name__}") from exc
        if response.status_code in _RETRYABLE:
            raise ChannelUnavailableError(f"MAX: {response.status_code}")
        if response.status_code >= 400:
            raise ChannelRejectedError(f"MAX отклонил запрос: {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise ChannelUnavailableError("MAX вернул не JSON") from exc

    # ------------------------------------------------------------- ChannelSender

    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        body = await self.call(
            str(credentials.get("token", "")),
            "POST",
            "/messages",
            params={"chat_id": delivery.chat_id, "disable_link_preview": "true"},
            json={"text": delivery.text[:MAX_MESSAGE_CHARS], "notify": True},
        )
        message = body.get("message") if isinstance(body, dict) else None
        mid = ((message or {}).get("body") or {}).get("mid")
        return str(mid) if mid else None

    # ------------------------------------------------------------- подключение

    async def me(self, token: str) -> dict[str, Any]:
        """Проверка токена при подключении: ID и имя бота."""
        body = await self.call(token, "GET", "/me")
        return body if isinstance(body, dict) else {}

    async def subscribe(self, token: str, url: str, secret: str) -> None:
        if not url.startswith("https://"):
            raise ChannelRejectedError("MAX принимает вебхуки только по HTTPS")
        if not _SECRET.fullmatch(secret):
            raise ChannelRejectedError("Секрет вебхука MAX: 5–256 символов A-Z, a-z, 0-9, _ и -")
        await self.call(
            token, "POST", "/subscriptions", json={"url": url, "update_types": UPDATE_TYPES, "secret": secret}
        )

    async def unsubscribe(self, token: str, url: str) -> None:
        await self.call(token, "DELETE", "/subscriptions", params={"url": url})
