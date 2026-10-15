"""Уведомления администратору и владельцу сервиса: передача диалога, запись «подтвердить вручную»,
превышение бюджета. Пока — в лог; в ноябре — Telegram-бот администратора (conversation/admin_bot.py).
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import structlog

logger = structlog.get_logger(__name__)


class AdminEventKind(StrEnum):
    HANDOFF = "handoff"
    BOOKING_PENDING = "booking_pending"
    BUDGET_EXCEEDED = "budget_exceeded"
    CLIENT_MESSAGE = "client_message"  # клиент пишет в диалог, который ведёт администратор
    UNSUPPORTED_MESSAGE = "unsupported_message"  # голосовое, стикер, файл
    DEAD_LETTER = "dead_letter"  # сообщение не обработано после всех повторов


@dataclass(frozen=True, slots=True)
class AdminEvent:
    kind: AdminEventKind
    salon_id: str
    conversation_id: str | None
    text: str  # для администратора салона; в логах телефоны и e-mail маскируются


class Notifier(Protocol):
    async def notify(self, event: AdminEvent) -> None: ...


class LogNotifier:
    async def notify(self, event: AdminEvent) -> None:
        logger.warning(
            "admin_notification",
            kind=event.kind.value,
            salon_id=event.salon_id,
            conversation_id=event.conversation_id,
            text=event.text,
        )
