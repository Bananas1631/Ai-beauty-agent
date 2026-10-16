"""Доменные события: «это произошло» — для независимых потребителей через Kafka (docs/events.md).

Задачи («сделай это») остаются в Celery и RabbitMQ; события читают несколько потребителей, каждый
своей группой, и журнал можно перечитать (ADR 013).

- Конверт одинаковый: event_id (идемпотентность потребителей), type, schema_version, salon_id
  (ключ сообщения Kafka — события салона идут в одну партицию по порядку), occurred_at и data.
- В data только ID, суммы, время и перечисления — персональных данных (имя, телефон, текст
  сообщения) нет: журнал событий хранится неделю и читается кем угодно из групп потребителей.
- Схемы — единственный источник правды и для сервиса на Kotlin (шаг 29): JSON Schema выгружается
  из этих моделей (scripts/export_event_schemas.py), тест следит, что выгрузка не устарела.
- Новое поле — с значением по умолчанию в той же версии схемы; удаление или смена смысла поля —
  новая версия (schema_version) и новый тип в Union, старые потребители читают старую.
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from beauty_agent.domain.enums import BookingStatus, Channel


class EventData(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BookingData(EventData):
    booking_id: uuid.UUID
    external_id: str | None = Field(None, max_length=64)  # ID записи в CRM — не персональные данные
    client_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None
    service_id: uuid.UUID
    master_id: uuid.UUID
    starts_at: datetime
    ends_at: datetime
    price: Decimal | None = None
    status: BookingStatus
    created_by_ai: bool


class BookingRescheduledData(BookingData):
    previous_starts_at: datetime
    previous_master_id: uuid.UUID


class BookingAttributedData(EventData):
    """Запись засчитана ИИ по правилу тарифа «Результат» (domain/billing.py): визит наступил."""

    booking_id: uuid.UUID
    starts_at: datetime
    price: Decimal | None = None
    status: BookingStatus
    billing_month: str = Field(pattern=r"^\d{4}-\d{2}$")  # месяц визита по времени салона


class MessageReceivedData(EventData):
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    client_id: uuid.UUID
    channel: Channel


class LlmCallFinishedData(EventData):
    """Ход агента завершён: модель, токены, стоимость. model=None — ответ без модели (кэш, guardrail)."""

    conversation_id: uuid.UUID
    model: str | None = None
    prompt_version: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_rub: Decimal = Field(ge=0)
    latency_ms: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    cache_hit: bool = False
    handed_off: bool = False
    guardrail: str | None = Field(None, max_length=64)


class DomainEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    schema_version: int = 1
    salon_id: uuid.UUID
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class BookingCreated(DomainEvent):
    type: Literal["booking.created"] = "booking.created"
    data: BookingData


class BookingRescheduled(DomainEvent):
    type: Literal["booking.rescheduled"] = "booking.rescheduled"
    data: BookingRescheduledData


class BookingCancelled(DomainEvent):
    type: Literal["booking.cancelled"] = "booking.cancelled"
    data: BookingData


class BookingAttributed(DomainEvent):
    type: Literal["booking.attributed"] = "booking.attributed"
    data: BookingAttributedData


class MessageReceived(DomainEvent):
    type: Literal["message.received"] = "message.received"
    data: MessageReceivedData


class LlmCallFinished(DomainEvent):
    type: Literal["llm.call.finished"] = "llm.call.finished"
    data: LlmCallFinishedData


Event = Annotated[
    BookingCreated | BookingRescheduled | BookingCancelled | BookingAttributed | MessageReceived | LlmCallFinished,
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)
EVENT_TYPES: tuple[type[DomainEvent], ...] = (
    BookingCreated,
    BookingRescheduled,
    BookingCancelled,
    BookingAttributed,
    MessageReceived,
    LlmCallFinished,
)

# Топик — по сущности: события одной записи (создана → перенесена → отменена) в одном топике,
# а ключ salon_id сохраняет их порядок внутри салона
TOPICS: dict[str, str] = {
    "booking.created": "beauty.bookings",
    "booking.rescheduled": "beauty.bookings",
    "booking.cancelled": "beauty.bookings",
    "booking.attributed": "beauty.bookings",
    "message.received": "beauty.messages",
    "llm.call.finished": "beauty.llm",
}


def topic_of(event: DomainEvent) -> str:
    return TOPICS[str(event.model_dump(include={"type"})["type"])]


def parse_event(raw: bytes | str) -> Event:
    """Событие из Kafka; неизвестный тип или поле не по схеме — ValidationError (уйдёт в DLQ)."""
    return EVENT_ADAPTER.validate_json(raw)
