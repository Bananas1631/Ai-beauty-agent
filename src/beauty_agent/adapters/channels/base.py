"""Общий интерфейс канала: разбор входящего обновления в единый формат и отправка ответа.

Сервис диалога одинаково работает с Telegram, Telegram Business, VK и MAX: адаптер канала
переводит обновление мессенджера в ChannelUpdate, а ответ отправляет через ChannelSender.
"""

from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from beauty_agent.domain.enums import Channel
from beauty_agent.domain.errors import DomainError

MAX_TEXT_CHARS = 4000


class UpdateKind(StrEnum):
    TEXT = "text"
    START = "start"  # /start, «Начать» — первое касание
    PHOTO = "photo"  # фото-референс; подпись — в text
    UNSUPPORTED = "unsupported"  # голосовое, стикер, файл


@dataclass(frozen=True, slots=True)
class ChannelUpdate:
    """Входящее сообщение клиента в едином формате."""

    channel: Channel
    update_id: str  # ID обновления для отсева повторов вебхука
    chat_id: str
    user_id: str
    message_id: str
    kind: UpdateKind
    text: str | None = None
    user_name: str | None = None
    route: dict[str, str] = field(default_factory=dict)  # параметры ответа: business_connection_id, peer_id
    photo: str | None = None  # file_id Telegram или https-ссылка VK и MAX; само фото не хранится

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["channel"] = self.channel.value
        data["kind"] = self.kind.value
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ChannelUpdate":
        return cls(
            channel=Channel(data["channel"]),
            update_id=str(data["update_id"]),
            chat_id=str(data["chat_id"]),
            user_id=str(data["user_id"]),
            message_id=str(data["message_id"]),
            kind=UpdateKind(data["kind"]),
            text=data.get("text"),
            user_name=data.get("user_name"),
            route=dict(data.get("route") or {}),
            photo=data.get("photo"),
        )


@dataclass(frozen=True, slots=True)
class Delivery:
    chat_id: str
    text: str
    route: dict[str, Any] = field(default_factory=dict)
    dedupe_key: str = ""  # для каналов с собственной идемпотентностью (random_id в VK)


class ChannelUnavailableError(DomainError):
    """Мессенджер не ответил или ограничил частоту — отправку стоит повторить позже."""

    default_message = "Мессенджер временно недоступен"


class ChannelRejectedError(DomainError):
    """Мессенджер отказал окончательно: клиент заблокировал бота, чат удалён, токен отозван."""

    default_message = "Мессенджер отклонил сообщение"


class ChannelSender(Protocol):
    async def send(self, credentials: dict[str, Any], delivery: Delivery) -> str | None:
        """Отправляет ответ; возвращает ID сообщения в мессенджере."""
        ...


def clip(text: str | None) -> str | None:
    """Обрезка входящего текста: длинные сообщения всё равно отсечёт guardrail, но в Redis хранить их незачем."""
    return text[:MAX_TEXT_CHARS] if text else text
