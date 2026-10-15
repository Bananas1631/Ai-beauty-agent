"""Правила, которые держит сама база: ни ошибка в коде, ни гонка запросов их не обойдут."""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from beauty_agent.db.models import Booking, Client, Conversation, Master, Message, Salon, Service
from beauty_agent.domain.enums import BookingStatus, Channel, MessageRole


@pytest.fixture
async def master_and_service(session: AsyncSession, salon: Salon) -> tuple[Master, Service]:
    master = Master(salon_id=salon.id, external_id="m-1", name="Анна")
    service = Service(
        salon_id=salon.id,
        external_id="s-1",
        name="Маникюр с покрытием",
        price_min=Decimal("2500.00"),
        price_max=Decimal("3000.00"),
        duration_min=90,
    )
    session.add_all([master, service])
    await session.commit()
    return master, service


def booking(
    master_and_service: tuple[Master, Service],
    start_hour: int,
    end_hour: int,
    *,
    status: BookingStatus = BookingStatus.CONFIRMED,
    key: str | None = None,
) -> Booking:
    master, service = master_and_service
    day = datetime(2026, 10, 20, tzinfo=UTC)
    return Booking(
        salon_id=master.salon_id,
        master_id=master.id,
        service_id=service.id,
        starts_at=day.replace(hour=start_hour),
        ends_at=day.replace(hour=end_hour),
        status=status,
        idempotency_key=key or uuid.uuid4().hex,
    )


async def test_overlapping_bookings_of_one_master_are_rejected(
    session: AsyncSession, master_and_service: tuple[Master, Service]
) -> None:
    session.add(booking(master_and_service, 10, 12))
    await session.commit()

    session.add(booking(master_and_service, 11, 13))
    with pytest.raises(IntegrityError, match="ex_bookings_master_overlap"):
        await session.commit()


async def test_back_to_back_and_cancelled_bookings_do_not_conflict(
    session: AsyncSession, master_and_service: tuple[Master, Service]
) -> None:
    session.add_all(
        [
            booking(master_and_service, 10, 11),
            booking(master_and_service, 11, 12),  # [10:00, 11:00) и [11:00, 12:00) не пересекаются
            booking(master_and_service, 14, 15, status=BookingStatus.CANCELLED),
            booking(master_and_service, 14, 15),  # отменённая запись время не держит
        ]
    )
    await session.commit()


async def test_same_idempotency_key_gives_one_booking(
    session: AsyncSession, master_and_service: tuple[Master, Service]
) -> None:
    session.add(booking(master_and_service, 10, 11, key="dialog-42-option-1"))
    await session.commit()

    session.add(booking(master_and_service, 16, 17, key="dialog-42-option-1"))
    with pytest.raises(IntegrityError, match="uq_bookings_salon_id_idempotency_key"):
        await session.commit()


async def test_repeated_webhook_does_not_duplicate_message(session: AsyncSession, salon: Salon) -> None:
    client = Client(salon_id=salon.id, name="Анна")
    session.add(client)
    await session.flush()
    conversation = Conversation(salon_id=salon.id, client_id=client.id, channel=Channel.TELEGRAM)
    session.add(conversation)
    await session.flush()

    for _ in range(2):  # мессенджер прислал одно и то же сообщение дважды
        session.add(
            Message(
                salon_id=salon.id,
                conversation_id=conversation.id,
                role=MessageRole.CLIENT,
                content="Хочу на маникюр",
                external_id="42",
            )
        )
    with pytest.raises(IntegrityError, match="uq_messages_conversation_id_external_id"):
        await session.commit()
