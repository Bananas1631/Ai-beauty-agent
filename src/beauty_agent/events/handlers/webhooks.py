"""Вебхуки партнёрам о записях салона (группа partner-webhooks).

Сервис записи не знает о партнёрах: он публикует booking.* в Kafka, а этот потребитель находит
активных OAuth-клиентов салона с адресом вебхука и отправляет им подписанное событие
(adapters/webhooks_out.py). Ошибка доставки — исключение: транзакция с отметкой о событии
откатывается, потребитель повторяет до 3 раз, затем событие уходит в DLQ и администратор получает
уведомление. Повтор может дойти до партнёра, который уже получил событие, — поэтому в заголовке
event_id, и получатель отсеивает дубли (так же, как наши потребители).
"""

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.repositories.oauth_clients import OAuthClientsRepository
from beauty_agent.domain.events import BookingCancelled, BookingCreated, BookingRescheduled, Event

GROUP = "partner-webhooks"
DELIVERED = (BookingCreated, BookingRescheduled, BookingCancelled)


class WebhookDelivery(Protocol):
    """adapters/webhooks_out.WebhookSender: подписать и отправить, не 2xx — исключение."""

    async def deliver(self, url: str, payload: Mapping[str, Any]) -> None: ...


class PartnerWebhooksHandler:
    def __init__(self, sender: WebhookDelivery, *, group: str = GROUP) -> None:
        self.sender = sender
        self._group = group

    @property
    def group(self) -> str:
        return self._group

    @property
    def topics(self) -> Sequence[str]:
        return ("beauty.bookings",)

    async def handle(self, session: AsyncSession, event: Event) -> None:
        if not isinstance(event, DELIVERED):
            return  # booking.attributed — внутреннее событие биллинга, партнёрам не уходит
        partners = await OAuthClientsRepository(session).webhooks(event.salon_id)
        payload = event.model_dump(mode="json")
        for partner in partners:
            if partner.webhook_url:
                await self.sender.deliver(partner.webhook_url, payload)
