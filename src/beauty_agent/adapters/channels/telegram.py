"""Адаптер Telegram-бота: разбор обновлений, отправка ответов, вебхук и long polling.

- Вебхук проверяет secret_token в заголовке X-Telegram-Bot-Api-Secret-Token (api/v1/webhooks.py).
- Принимаются только личные чаты: группы и каналы агенту не нужны.
- Голосовые, стикеры, фото и файлы — UNSUPPORTED: клиенту вежливый ответ, администратору уведомление.
- Режим Telegram Business (бот отвечает от имени аккаунта салона) — в telegram_business.py.
Токен бота стоит в пути URL, поэтому проверяется по формату и не пишется в логи.
"""

import re
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
from beauty_agent.adapters.channels.telegram_business import business_client_message
from beauty_agent.domain.enums import Channel

API_URL = "https://api.telegram.org"
MAX_MESSAGE_CHARS = 4096
_TOKEN = re.compile(r"^\d{5,15}:[A-Za-z0-9_-]{30,64}$")
_ALLOWED_UPDATES = ["message", "business_message"]


def check_token(token: str) -> str:
    if not _TOKEN.fullmatch(token):
        raise ChannelRejectedError("Неверный формат токена Telegram-бота")
    return token


def parse_update(payload: dict[str, Any]) -> ChannelUpdate | None:
    """Обновление Telegram → ChannelUpdate. None — обновление агенту не нужно."""
    update_id = payload.get("update_id")
    if "business_message" in payload:
        message = business_client_message(payload["business_message"])
        channel = Channel.TELEGRAM_BUSINESS
    else:
        message = payload.get("message")
        channel = Channel.TELEGRAM
    if update_id is None or not isinstance(message, dict):
        return None  # редактирования, нажатия кнопок, служебные события
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    if chat.get("type") != "private" or sender.get("is_bot"):
        return None
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        kind = UpdateKind.START if text.strip().startswith("/start") else UpdateKind.TEXT
    else:
        kind = UpdateKind.UNSUPPORTED
    route = (
        {"business_connection_id": str(message["business_connection_id"])}
        if channel is Channel.TELEGRAM_BUSINESS
        else {}
    )
    return ChannelUpdate(
        channel=channel,
        update_id=str(update_id),
        chat_id=str(chat["id"]),
        user_id=str(sender.get("id", chat["id"])),
        message_id=str(message["message_id"]),
        kind=kind,
        text=clip(text) if kind is not UpdateKind.UNSUPPORTED else None,
        user_name=(sender.get("first_name") or None),
        route=route,
    )


class TelegramBotApi:
    def __init__(self, http: httpx.AsyncClient, *, base_url: str = API_URL) -> None:
        self.http = http
        self.base_url = base_url

    async def call(self, token: str, method: str, payload: dict[str, Any], *, timeout_s: float = 15.0) -> Any:
        url = f"{self.base_url}/bot{check_token(token)}/{method}"
        try:
            response = await self.http.post(url, json=payload, timeout=timeout_s)
        except httpx.HTTPError as exc:
            raise ChannelUnavailableError(f"Telegram недоступен: {type(exc).__name__}") from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise ChannelUnavailableError("Telegram вернул не JSON") from exc
        if body.get("ok"):
            return body.get("result")
        code = int(body.get("error_code") or response.status_code)
        if code == 429 or code >= 500:
            raise ChannelUnavailableError(f"Telegram: {code}")
        raise ChannelRejectedError(f"Telegram отклонил запрос: {code}")

    # ------------------------------------------------------------- ChannelSender

    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        payload: dict[str, Any] = {"chat_id": delivery.chat_id, "text": delivery.text[:MAX_MESSAGE_CHARS]}
        connection = delivery.route.get("business_connection_id")
        if connection:
            payload["business_connection_id"] = connection  # ответ от имени аккаунта салона
        result = await self.call(str(credentials.get("token", "")), "sendMessage", payload)
        return str(result.get("message_id")) if isinstance(result, dict) else None

    # ------------------------------------------------------------- вебхук и polling

    async def set_webhook(self, token: str, url: str, secret_token: str) -> None:
        await self.call(
            token,
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": _ALLOWED_UPDATES,
                "drop_pending_updates": False,
            },
        )

    async def delete_webhook(self, token: str) -> None:
        await self.call(token, "deleteWebhook", {"drop_pending_updates": False})

    async def get_updates(self, token: str, offset: int | None, *, timeout_s: int = 25) -> list[dict[str, Any]]:
        """Long polling для локальной разработки: на ноутбуке нет публичного HTTPS для вебхука."""
        payload: dict[str, Any] = {"timeout": timeout_s, "allowed_updates": _ALLOWED_UPDATES}
        if offset is not None:
            payload["offset"] = offset
        result = await self.call(token, "getUpdates", payload, timeout_s=timeout_s + 10)
        return list(result or [])
